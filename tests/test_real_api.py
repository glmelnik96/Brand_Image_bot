"""
Phase 3: реальные вызовы Phygital+ API по всем сценариям бота.

Бьём API напрямую через PhygitalClient (harness-level, без Telegram), используя
session.json из storage/. Каждый сценарий → отдельная папка с артефактами:

    tests/results/run-{YYYYMMDD-HHMMSS}/{scenario}/
        result.png        — финальный image (или result_N.png если несколько)
        prompt.txt        — итоговый prompt(ы), включая Gemini description если он был
        status_log.json   — список progress-event'ов + финальный статус job'а
        timing.json       — wall-clock тайминги: start, end, total_sec, per_step

После всех сценариев — `summary.json` со списком scenario + статус + затраченные кредиты.

Запуск:
    .venv/Scripts/python.exe -m tests.test_real_api

Бюджет: ориентир ~2000 кредитов. Реальный расход обычно ~100-300 (см. summary).

Если session.json просрочен / refresh не сработает — упадёт PhygitalAuthError; запускать
после `python -m recon.capture` (или ручного логина) если так.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import truststore
import ssl
from loguru import logger

# Project root → sys.path для импорта модулей bot/client/workflows
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from client.api import PhygitalClient  # noqa: E402
from client.config import settings  # noqa: E402
from client.models import GenerationJob  # noqa: E402
from client.session import SessionManager  # noqa: E402
from workflows.brand_img2img import run_brand_img2img  # noqa: E402
from workflows.brand_text2img import run_brand_text2img  # noqa: E402
from workflows.image_gen import ImageGenWorkflow  # noqa: E402
from workflows.image_to_image import ImageToImageWorkflow  # noqa: E402
from workflows.speaker_prep import build_speaker_prep_workflow, speaker_prompt  # noqa: E402

INPUT_DIR = ROOT / "input"
RESULTS_DIR = ROOT / "tests" / "results"

# Стандартные test-inputs
REF_SPEAKER = INPUT_DIR / "input speaker image.png"
EXAMPLE_MAN = INPUT_DIR / "speaker example_man.jpg"
EXAMPLE_WOMAN = INPUT_DIR / "speaker example_woman.jpg"


# ── helpers ─────────────────────────────────────────────────────────────────

_SSL_CTX = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


async def _download(url: str, dest: Path) -> int:
    """Скачать image-результат. Возвращает byte size."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(verify=_SSL_CTX, timeout=60.0) as cli:
        r = await cli.get(url)
        r.raise_for_status()
        dest.write_bytes(r.content)
        return len(r.content)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _save_artifacts(
    scenario_dir: Path,
    *,
    job: GenerationJob,
    prompt_text: str,
    progress_events: list[dict[str, Any]],
    timing: dict[str, Any],
) -> list[Path]:
    """Сохранить prompt.txt + status_log.json + timing.json. Возвращает [downloaded paths]."""
    scenario_dir.mkdir(parents=True, exist_ok=True)
    (scenario_dir / "prompt.txt").write_text(prompt_text, encoding="utf-8")
    (scenario_dir / "status_log.json").write_text(
        json.dumps(
            {
                "events": progress_events,
                "final": {
                    "job_id": job.job_id,
                    "status": job.status,
                    "error": job.error,
                    "result_urls": job.result_urls,
                },
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (scenario_dir / "timing.json").write_text(
        json.dumps(timing, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return []


async def _download_results(scenario_dir: Path, job: GenerationJob) -> list[Path]:
    files: list[Path] = []
    if job.status != "completed":
        return files
    for i, url in enumerate(job.result_urls):
        ext = ".png"
        # Берём расширение из URL если есть
        url_lower = url.lower().split("?")[0]
        for cand in (".png", ".jpg", ".jpeg", ".webp"):
            if url_lower.endswith(cand):
                ext = cand
                break
        name = f"result_{i+1}{ext}" if len(job.result_urls) > 1 else f"result{ext}"
        dest = scenario_dir / name
        size = await _download(url, dest)
        logger.info(f"[{scenario_dir.name}] downloaded {dest.name} ({size} bytes)")
        files.append(dest)
    return files


class _ProgressLogger:
    """Адаптер progress_cb → list[dict]. Шлёт wall-clock дельты для timing.json."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._t0 = time.monotonic()
        self._last_step: str | None = None
        self._last_step_start = self._t0

    async def __call__(self, step: str) -> None:
        now = time.monotonic()
        elapsed_from_start = now - self._t0
        prev_step_dur = now - self._last_step_start if self._last_step else None
        evt = {
            "at_sec": round(elapsed_from_start, 2),
            "step": step,
            "prev_step": self._last_step,
            "prev_step_dur_sec": round(prev_step_dur, 2) if prev_step_dur is not None else None,
            "wall_clock": _now_iso(),
        }
        self.events.append(evt)
        logger.info(f"[progress] {step} (t+{elapsed_from_start:.1f}s)")
        self._last_step = step
        self._last_step_start = now

    def finalize(self, final_status: str) -> dict[str, Any]:
        now = time.monotonic()
        total = now - self._t0
        return {
            "started_at": _now_iso(),
            "total_sec": round(total, 2),
            "last_step": self._last_step,
            "last_step_dur_sec": round(now - self._last_step_start, 2) if self._last_step else None,
            "final_status": final_status,
            "events_count": len(self.events),
        }


# ── scenarios ───────────────────────────────────────────────────────────────


async def run_scenario_nb_t2i(client: PhygitalClient, run_dir: Path) -> dict[str, Any]:
    """Plain Nano Banana 3.1 text→image. Простой prompt, default ratio/res."""
    scenario = "nb_t2i"
    sdir = run_dir / scenario
    pl = _ProgressLogger()
    prompt = "a cute cartoon orange cat sitting on a windowsill at sunrise, soft pastel colors"

    # ImageGenWorkflow прогрессом не отчитывается — фиксируем единственный шаг.
    await pl("Nano Banana")
    wf = ImageGenWorkflow(client, model_name="v3_1", ratio="default", resolution="default")
    job = await wf.run(prompt=prompt)
    timing = pl.finalize(job.status)
    _save_artifacts(
        sdir,
        job=job,
        prompt_text=f"=== USER PROMPT ===\n{prompt}",
        progress_events=pl.events,
        timing=timing,
    )
    await _download_results(sdir, job)
    return _summary(scenario, job, timing)


async def run_scenario_nb_i2i(client: PhygitalClient, run_dir: Path) -> dict[str, Any]:
    """Plain Nano Banana 3.1 image→image. Используем speaker example_man.jpg как init."""
    scenario = "nb_i2i"
    sdir = run_dir / scenario
    pl = _ProgressLogger()
    prompt = "transform into a watercolor painting, soft brush strokes, paper texture"

    await pl("Nano Banana")
    wf = ImageToImageWorkflow(client, model_name="v3_1", ratio="r_3_4", resolution="k1")
    job = await wf.run_with_files(prompt=prompt, init_paths=[EXAMPLE_MAN])
    timing = pl.finalize(job.status)
    _save_artifacts(
        sdir,
        job=job,
        prompt_text=f"=== USER PROMPT ===\n{prompt}\n\n=== INIT IMAGE ===\n{EXAMPLE_MAN.name}",
        progress_events=pl.events,
        timing=timing,
    )
    await _download_results(sdir, job)
    return _summary(scenario, job, timing)


async def run_scenario_brand_t2i(
    client: PhygitalClient,
    run_dir: Path,
    *,
    variant: str,
    user_prompt: str,
) -> dict[str, Any]:
    """Brand text→image для каждого variant: photo/render/isometric."""
    scenario = f"brand_t2i_{variant}"
    sdir = run_dir / scenario
    pl = _ProgressLogger()
    await pl("Gemini Text")
    job = await run_brand_text2img(
        client,
        prompt=user_prompt,
        variant=variant,
        model_name="v3_1",
        ratio="default",
        resolution="default",
        progress_cb=pl,
    )
    timing = pl.finalize(job.status)
    # Gemini description можно вытащить из job.raw если есть; иначе только user prompt.
    gemini_desc = ""
    if isinstance(job.raw, dict) and "gemini" in job.raw:
        gemini_desc = str(job.raw.get("gemini", ""))[:5000]
    _save_artifacts(
        sdir,
        job=job,
        prompt_text=(
            f"=== USER PROMPT (variant={variant}) ===\n{user_prompt}\n\n"
            f"=== GEMINI DESCRIPTION (если сохранилось в job.raw) ===\n{gemini_desc or '— нет в raw —'}"
        ),
        progress_events=pl.events,
        timing=timing,
    )
    await _download_results(sdir, job)
    return _summary(scenario, job, timing)


async def run_scenario_brand_i2i(client: PhygitalClient, run_dir: Path) -> dict[str, Any]:
    """Brand image→image: Gemini Text describes the image (brand-style) → Nano Banana redraws."""
    scenario = "brand_i2i"
    sdir = run_dir / scenario
    pl = _ProgressLogger()
    await pl("Gemini Text")
    job = await run_brand_img2img(
        client,
        init_paths=[EXAMPLE_MAN],
        model_name="v3_1",
        ratio="r_3_4",
        resolution="k1",
        progress_cb=pl,
    )
    timing = pl.finalize(job.status)
    _save_artifacts(
        sdir,
        job=job,
        prompt_text=f"=== INIT IMAGE ===\n{EXAMPLE_MAN.name}\n\n=== (prompt = Gemini description) ===",
        progress_events=pl.events,
        timing=timing,
    )
    await _download_results(sdir, job)
    return _summary(scenario, job, timing)


async def run_scenario_speaker(client: PhygitalClient, run_dir: Path) -> dict[str, Any]:
    """Speaker portrait prep: reference + user photo → unified studio portrait."""
    scenario = "speaker"
    sdir = run_dir / scenario
    pl = _ProgressLogger()
    await pl("Nano Banana")
    wf = build_speaker_prep_workflow(client)
    prompt = speaker_prompt("man")
    # Order from cli.py: reference first, user photo second
    job = await wf.run_with_files(prompt=prompt, init_paths=[REF_SPEAKER, EXAMPLE_MAN])
    timing = pl.finalize(job.status)
    _save_artifacts(
        sdir,
        job=job,
        prompt_text=(
            f"=== SPEAKER PROMPT (man) — first 500 chars ===\n{prompt[:500]}\n...\n\n"
            f"=== INPUTS ===\nreference: {REF_SPEAKER.name}\nuser_photo: {EXAMPLE_MAN.name}"
        ),
        progress_events=pl.events,
        timing=timing,
    )
    await _download_results(sdir, job)
    return _summary(scenario, job, timing)


async def run_scenario_safety_retry(client: PhygitalClient, run_dir: Path) -> dict[str, Any]:
    """Проверяем live safety-retry: подаём prompt со словом 'knob', которое Nano Banana
    раньше блокировал. Ждём что: либо Gemini сразу его уберёт в description (Cloud.ru
    enhancer тоже видит этот мусор), либо retry-loop его вычистит. Главное — final job
    должен быть completed."""
    scenario = "safety_retry"
    sdir = run_dir / scenario
    pl = _ProgressLogger()
    user_prompt = "data center server rack with a brass knob handle, isometric view"
    await pl("Gemini Text")
    job = await run_brand_text2img(
        client,
        prompt=user_prompt,
        variant="isometric",
        model_name="v3_1",
        ratio="default",
        resolution="default",
        progress_cb=pl,
    )
    timing = pl.finalize(job.status)
    _save_artifacts(
        sdir,
        job=job,
        prompt_text=f"=== USER PROMPT (intentionally triggers 'knob' safety) ===\n{user_prompt}",
        progress_events=pl.events,
        timing=timing,
    )
    await _download_results(sdir, job)
    return _summary(scenario, job, timing)


# ── runner ──────────────────────────────────────────────────────────────────


def _summary(scenario: str, job: GenerationJob, timing: dict[str, Any]) -> dict[str, Any]:
    return {
        "scenario": scenario,
        "status": job.status,
        "job_id": job.job_id,
        "error": job.error,
        "total_sec": timing.get("total_sec"),
        "events_count": timing.get("events_count"),
        "result_files": len(job.result_urls) if job.status == "completed" else 0,
    }


async def main() -> int:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RESULTS_DIR / f"run-{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"=== Phase 3 real-API run @ {run_dir} ===")

    manager = SessionManager(settings.session_file)
    session = manager.load()
    if session is None:
        logger.error(f"No session at {settings.session_file}. Run recon.capture first.")
        return 2

    summaries: list[dict[str, Any]] = []
    async with PhygitalClient(session, session_manager=manager) as client:
        # Порядок: дешёвые без brand → дорогие с brand → safety retry в конце.
        scenarios: list[tuple[str, Callable[[], Awaitable[dict[str, Any]]]]] = [
            ("nb_t2i", lambda: run_scenario_nb_t2i(client, run_dir)),
            ("nb_i2i", lambda: run_scenario_nb_i2i(client, run_dir)),
            ("brand_t2i_photo", lambda: run_scenario_brand_t2i(
                client, run_dir, variant="photo",
                user_prompt="a Cloud.ru data center engineer talking to a colleague at a server rack",
            )),
            ("brand_t2i_render", lambda: run_scenario_brand_t2i(
                client, run_dir, variant="render",
                user_prompt="abstract render of cloud infrastructure with floating compute units",
            )),
            ("brand_t2i_isometric", lambda: run_scenario_brand_t2i(
                client, run_dir, variant="isometric",
                user_prompt="isometric scene of a developer deploying code to the cloud",
            )),
            ("brand_i2i", lambda: run_scenario_brand_i2i(client, run_dir)),
            ("speaker", lambda: run_scenario_speaker(client, run_dir)),
            ("safety_retry", lambda: run_scenario_safety_retry(client, run_dir)),
        ]
        for name, fn in scenarios:
            logger.info(f"\n━━━ Scenario: {name} ━━━")
            try:
                s = await fn()
            except Exception as e:
                logger.opt(exception=e).error(f"[{name}] crashed")
                s = {"scenario": name, "status": "crashed", "error": f"{type(e).__name__}: {e}"}
            summaries.append(s)
            logger.info(f"[{name}] status={s.get('status')} t={s.get('total_sec')}s")

    summary_path = run_dir / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "started_at": ts,
                "scenarios": summaries,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    logger.info(f"=== Summary saved → {summary_path} ===")
    ok = sum(1 for s in summaries if s.get("status") == "completed")
    print(f"\n{ok}/{len(summaries)} scenarios completed. Results: {run_dir}")
    return 0 if ok == len(summaries) else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
