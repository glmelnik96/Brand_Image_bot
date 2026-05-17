"""
Smoke-test: end-to-end генерация через клиент.

Логика сессии:
  - Если есть storage/session.json — используем её (продлевается через refresh).
  - Иначе — bootstrap из последнего recon/captures/storage-*.json и сохраняем.

Запуск (после активации venv):
    python -m recon.smoke_test "your prompt here"
"""

from __future__ import annotations

import asyncio
import ssl
import sys
from pathlib import Path

import httpx
import truststore
from loguru import logger

from client.api import PhygitalClient
from client.config import settings
from client.session import SessionManager
from workflows.image_gen import ImageGenWorkflow

ROOT = Path(__file__).resolve().parent.parent
CAPTURES = ROOT / "recon" / "captures"
RESULTS = ROOT / "recon" / "results"

_SSL_CTX = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def latest_storage_dump() -> Path:
    files = sorted(CAPTURES.glob("storage-*.json"))
    if not files:
        raise SystemExit("Нет storage-*.json в recon/captures/. Запусти recon/capture.py.")
    return files[-1]


def load_or_bootstrap(manager: SessionManager):
    """Загружает session.json, либо создаёт из последнего recon-дампа."""
    s = manager.load()
    if s and s.access_token:
        return s
    dump = latest_storage_dump()
    logger.info(f"Bootstrap session from {dump}")
    s = SessionManager.from_recon_dump(dump)
    manager.save(s)
    return s


async def download(url: str, dest: Path) -> None:
    async with httpx.AsyncClient(timeout=120, follow_redirects=True, verify=_SSL_CTX) as c:
        r = await c.get(url)
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        logger.success(f"Downloaded → {dest} ({len(r.content)} bytes)")


async def main() -> None:
    prompt = " ".join(sys.argv[1:]).strip() or (
        "A photorealistic studio portrait of a friendly tabby cat sitting on a "
        "wooden desk, soft natural window light, shallow depth of field, 50mm lens."
    )

    manager = SessionManager(settings.session_file)
    session = load_or_bootstrap(manager)

    async with PhygitalClient(session, session_manager=manager) as client:
        wf = ImageGenWorkflow(client)
        logger.info(f"Prompt: {prompt[:120]}{'...' if len(prompt)>120 else ''}")
        job = await wf.run(prompt=prompt)

    logger.info(f"Result: status={job.status} urls={len(job.result_urls)}")
    if job.status != "completed":
        logger.error(f"Failed: {job.error}")
        return

    for i, url in enumerate(job.result_urls):
        links = job.raw.get("links", [])
        name = links[i]["file_name"] if i < len(links) and links[i].get("file_name") else f"result_{i}.jpg"
        await download(url, RESULTS / f"task_{job.job_id}_{name}")


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | {level: <7} | {message}")
    asyncio.run(main())
