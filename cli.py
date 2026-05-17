"""
Phygital+ CLI.

Примеры:
    python -m cli generate "a cat on a desk"
    python -m cli generate "neon city street at night" --count 3 --output ./out
    python -m cli generate "..." --model v3 --ratio 1x1 --resolution k2
    python -m cli session info
    python -m cli session refresh
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Под Windows stdout/stderr по умолчанию cp1251 — Unicode-эмодзи/стрелки в выводе
# валят cli с UnicodeEncodeError. Переключаем явно.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass

import httpx
import truststore
from loguru import logger

from client.api import PhygitalClient
from client.config import settings
from client.session import FRONT_TOKEN_COOKIE, Session, SessionManager
from workflows.image_gen import ImageGenWorkflow
from workflows.image_to_image import ImageToImageWorkflow
from workflows.speaker_prep import (
    DEFAULT_REFERENCE,
    build_speaker_prep_workflow,
    detect_gender_from_filename,
    speaker_prompt,
)

ROOT = Path(__file__).resolve().parent
CAPTURES = ROOT / "recon" / "captures"
DEFAULT_OUTPUT = ROOT / "outputs"

_SSL_CTX = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


# ── session bootstrap ─────────────────────────────────────────────────────
def _latest_recon_dump() -> Path | None:
    files = sorted(CAPTURES.glob("storage-*.json"))
    return files[-1] if files else None


def load_session(manager: SessionManager) -> Session:
    """Берём persisted session, иначе bootstrap из последнего recon-дампа."""
    s = manager.load()
    if s and s.access_token:
        return s
    dump = _latest_recon_dump()
    if not dump:
        raise SystemExit(
            "Нет ни storage/session.json, ни recon/captures/storage-*.json.\n"
            "Запусти сначала: python -m recon.capture"
        )
    logger.info(f"Bootstrap session from {dump}")
    s = SessionManager.from_recon_dump(dump)
    manager.save(s)
    return s


# ── helpers ───────────────────────────────────────────────────────────────
def _decode_front_token(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    try:
        return json.loads(base64.b64decode(token + "=" * (-len(token) % 4)))
    except Exception:
        return None


async def _download(url: str, dest: Path) -> int:
    async with httpx.AsyncClient(timeout=180, follow_redirects=True, verify=_SSL_CTX) as c:
        r = await c.get(url)
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return len(r.content)


# ── commands ──────────────────────────────────────────────────────────────
async def cmd_generate(args: argparse.Namespace) -> int:
    prompt: str = args.prompt
    count: int = args.count
    output: Path = Path(args.output).expanduser().resolve()
    init_paths: list[Path] = [Path(p).expanduser().resolve() for p in (args.init_img or [])]

    manager = SessionManager(settings.session_file)
    session = load_session(manager)

    async def one(idx: int) -> tuple[int, list[Path]]:
        # Каждой задаче — свой клиент (одна httpx-сессия = одно соединение).
        # Все они шарят один Session-объект и менеджер (refresh защищён локом).
        async with PhygitalClient(session, session_manager=manager) as client:
            if init_paths:
                wf: ImageGenWorkflow = ImageToImageWorkflow(
                    client,
                    model_name=args.model if args.model != "v3" else "v3_1",
                    ratio=args.ratio if args.ratio != "default" else "r_3_4",
                    resolution=args.resolution,
                )
                logger.info(f"[{idx}] uploading {len(init_paths)} init images + submitting…")
                job = await wf.run_with_files(prompt=prompt, init_paths=init_paths)  # type: ignore[attr-defined]
            else:
                wf = ImageGenWorkflow(
                    client,
                    model_name=args.model,
                    ratio=args.ratio,
                    resolution=args.resolution,
                )
                logger.info(f"[{idx}] submitting…")
                job = await wf.run(prompt=prompt)
            if job.status != "completed":
                logger.error(f"[{idx}] failed: {job.error}")
                return idx, []
            files: list[Path] = []
            links = job.raw.get("links", [])
            for i, url in enumerate(job.result_urls):
                name = links[i]["file_name"] if i < len(links) and links[i].get("file_name") else f"result_{i}.jpg"
                dest = output / f"task_{job.job_id}_{idx}_{name}"
                size = await _download(url, dest)
                logger.success(f"[{idx}] → {dest} ({size} bytes)")
                files.append(dest)
            return idx, files

    results = await asyncio.gather(*(one(i) for i in range(count)), return_exceptions=True)

    ok = 0
    for r in results:
        if isinstance(r, Exception):
            logger.opt(exception=r).error(f"Task crashed: {type(r).__name__}: {r}")
        elif r[1]:
            ok += 1

    logger.info(f"Done: {ok}/{count} succeeded")
    return 0 if ok == count else 1


async def cmd_prep_speaker(args: argparse.Namespace) -> int:
    photo = Path(args.photo).expanduser().resolve()
    reference = Path(args.reference).expanduser().resolve()
    output: Path = Path(args.output).expanduser().resolve()
    count: int = args.count

    if not photo.exists():
        logger.error(f"Photo not found: {photo}")
        return 2
    if not reference.exists():
        logger.error(f"Reference not found: {reference}")
        return 2

    gender = args.gender or detect_gender_from_filename(photo)
    if not gender:
        logger.error("Could not detect gender from filename. Pass --gender man|woman.")
        return 2

    prompt = args.prompt or speaker_prompt(gender)
    # Порядок входов как в recon: reference-картинка с red square первая, фото спикера — вторая.
    init_paths = [reference, photo]

    manager = SessionManager(settings.session_file)
    session = load_session(manager)

    async def one(idx: int) -> tuple[int, list[Path]]:
        async with PhygitalClient(session, session_manager=manager) as client:
            wf = build_speaker_prep_workflow(client)
            logger.info(f"[{idx}] prep-speaker gender={gender} photo={photo.name}…")
            job = await wf.run_with_files(prompt=prompt, init_paths=init_paths)
            if job.status != "completed":
                logger.error(f"[{idx}] failed: {job.error}")
                return idx, []
            files: list[Path] = []
            links = job.raw.get("links", [])
            for i, url in enumerate(job.result_urls):
                name = links[i]["file_name"] if i < len(links) and links[i].get("file_name") else f"result_{i}.jpg"
                dest = output / f"speaker_{gender}_{job.job_id}_{idx}_{name}"
                size = await _download(url, dest)
                logger.success(f"[{idx}] → {dest} ({size} bytes)")
                files.append(dest)
            return idx, files

    results = await asyncio.gather(*(one(i) for i in range(count)), return_exceptions=True)
    ok = sum(1 for r in results if not isinstance(r, Exception) and r[1])
    for r in results:
        if isinstance(r, Exception):
            logger.opt(exception=r).error(f"Task crashed: {type(r).__name__}: {r}")
    logger.info(f"Done: {ok}/{count} succeeded")
    return 0 if ok == count else 1


async def cmd_session_info(_args: argparse.Namespace) -> int:
    manager = SessionManager(settings.session_file)
    s = manager.load()
    if not s:
        print(f"No session at {settings.session_file}")
        print("Run: python -m recon.capture")
        return 1

    print(f"Session file: {settings.session_file}")
    print(f"Captured at:  {s.captured_at.isoformat()}")
    print(f"access_token: len={len(s.access_token) if s.access_token else 0}")
    print(f"refresh_token: len={len(s.refresh_token) if s.refresh_token else 0}")

    front = _decode_front_token(s.cookie_value(FRONT_TOKEN_COOKIE))
    if front and "ate" in front:
        expiry_ms = int(front["ate"])
        expiry_dt = datetime.fromtimestamp(expiry_ms / 1000, tz=timezone.utc)
        now_ms = int(time.time() * 1000)
        ttl_sec = (expiry_ms - now_ms) // 1000
        status = "valid" if ttl_sec > 0 else "EXPIRED"
        print(f"access expires at: {expiry_dt.isoformat()} ({status}, ttl={ttl_sec}s)")
        if "uid" in front:
            print(f"user id: {front['uid']}")
    else:
        print("No front-token info available (expiry unknown)")
    return 0


async def cmd_session_refresh(_args: argparse.Namespace) -> int:
    manager = SessionManager(settings.session_file)
    s = manager.load()
    if not s:
        print(f"No session at {settings.session_file}. Run: python -m recon.capture")
        return 1
    before = s.access_token
    await manager.refresh(s)
    print(f"Refresh OK. access_token changed: {s.access_token != before}")
    return 0


# ── argparse ──────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="phygital", description="Phygital+ automation CLI")
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG logs")
    sp = p.add_subparsers(dest="cmd", required=True)

    g = sp.add_parser("generate", help="Generate image(s) from a prompt")
    g.add_argument("prompt", help="Text prompt")
    g.add_argument("-n", "--count", type=int, default=1, help="Number of parallel generations")
    g.add_argument("--model", default="v3", help="model_name param (default: v3)")
    g.add_argument("--ratio", default="default", help="ratio param (default: default)")
    g.add_argument("--resolution", default="k2", help="resolution param (default: k2)")
    g.add_argument("-o", "--output", default=str(DEFAULT_OUTPUT), help=f"Output dir (default: {DEFAULT_OUTPUT})")
    g.add_argument(
        "--init-img",
        action="append",
        help="Path to init image (img2img). Repeat the flag to pass multiple. "
             "When set, model/ratio default to v3_1 / r_3_4.",
    )

    ps = sp.add_parser("prep-speaker", help="Speaker portrait prep preset (Nano Banana 3.1, 3:4, 2K)")
    ps.add_argument("photo", help="Speaker photo (jpg/png)")
    ps.add_argument(
        "--reference",
        default=str(DEFAULT_REFERENCE),
        help=f"Reference pose photo (default: {DEFAULT_REFERENCE})",
    )
    ps.add_argument("--gender", choices=["man", "woman"], help="man|woman (auto-detected from filename otherwise)")
    ps.add_argument("--prompt", help="Override default speaker-prep prompt")
    ps.add_argument("-n", "--count", type=int, default=1, help="Number of parallel generations")
    ps.add_argument("-o", "--output", default=str(DEFAULT_OUTPUT), help=f"Output dir (default: {DEFAULT_OUTPUT})")

    s = sp.add_parser("session", help="Session management")
    ssp = s.add_subparsers(dest="action", required=True)
    ssp.add_parser("info", help="Show current session status")
    ssp.add_parser("refresh", help="Force refresh access token")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logger.remove()
    level = "DEBUG" if args.verbose else "INFO"
    logger.add(sys.stderr, level=level, format="<green>{time:HH:mm:ss}</green> | {level: <7} | {message}")

    if args.cmd == "generate":
        coro = cmd_generate(args)
    elif args.cmd == "prep-speaker":
        coro = cmd_prep_speaker(args)
    elif args.cmd == "session":
        coro = {"info": cmd_session_info, "refresh": cmd_session_refresh}[args.action](args)
    else:
        parser.error(f"unknown command {args.cmd}")
        return 2

    return asyncio.run(coro)


if __name__ == "__main__":
    raise SystemExit(main())
