"""
Who is who — text→4 images через цепочку Gemini Flash → Midjourney /imagine.

Pipeline:
  user_text → Gemini Flash (system_prompt=SYSTEM_PROMPT_WhoIsWho.md, thinking=low)
            → перевод словосочетаний на английский + случайная перетасовка слов
            → MJ /imagine (4 картинки в grid)

Gemini Flash тут используется намеренно (не Pro): нужно быстрое детерминированное
переписывание входной строки, а не глубокий энхансер. Совпадает с UI Phygital+,
где у Who-is-who сценария стоит флэш-модель с thinking_level=low.

Возвращает финальный GenerationJob от MidJourneyImagineWorkflow:
  job.result_urls — 4 ссылки на S3 (image_grid)
  job.job_id     — task_id MJ Imagine (нужен для последующих Upscale/Variation U/V)
  job.raw["mj_task_id"] — то же значение int (удобно для bot/scenarios.py)
  job.raw["gemini_prompt"] — текст после Flash (для логов/отладки, юзеру НЕ показываем)

Если Gemini Flash упал — возвращаем FAILED job с error="Gemini Flash: ...".
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

from loguru import logger

from client.api import PhygitalClient
from client.models import GenerationJob
from workflows.base import ProgressCallback
from workflows.brand_docs import WHOISWHO_DOC, get_whoiswho_doc, invalidate_brand_doc
from workflows.gemini_text import GeminiTextWorkflow
from workflows.midjourney import MidJourneyImagineWorkflow

ProgressCb = Optional[Callable[[str], Awaitable[None]]]
PctCb = Optional[ProgressCallback]


def _is_stale_doc_error(job: GenerationJob) -> bool:
    if job.status == "completed":
        return False
    err = (job.error or "").lower()
    return "cannot upload files" in err


async def _run_flash(
    client: PhygitalClient,
    *,
    prompt: str,
    doc_id: int,
    pct_cb: PctCb,
) -> GenerationJob:
    """Gemini Flash с фиксированной комбинацией (flash_3 + thinking=low).
    Flash-fallback тут не нужен — мы УЖЕ на Flash."""
    wf = GeminiTextWorkflow(client, model="flash_3", thinking_level="low")
    wf.on_progress = pct_cb
    return await wf.run_text(prompt=prompt, document_ids=[doc_id])


async def run_who_is_who(
    client: PhygitalClient,
    *,
    prompt: str,
    aspect_ratio: str = "square",
    progress_cb: ProgressCb = None,
    pct_cb: PctCb = None,
) -> GenerationJob:
    """Полный сценарий: user prompt → Gemini Flash (WhoIsWho) → MJ Imagine.

    Возвращает GenerationJob от MJ Imagine (result_urls = 4 ссылки).
    На фейле Gemini — failed job с error="Gemini Flash: ...".
    """
    doc_id = await get_whoiswho_doc(client)
    logger.info(f"[wiw] using WhoIsWho doc_id={doc_id}")

    job_g = await _run_flash(client, prompt=prompt, doc_id=doc_id, pct_cb=pct_cb)
    if _is_stale_doc_error(job_g):
        logger.warning(
            f"[wiw] Phygital reported stale WhoIsWho doc (was id={doc_id}); "
            f"invalidating cache and retrying Gemini Flash once"
        )
        await invalidate_brand_doc(WHOISWHO_DOC)
        doc_id = await get_whoiswho_doc(client)
        logger.info(f"[wiw] retry Gemini Flash with fresh doc_id={doc_id}")
        job_g = await _run_flash(client, prompt=prompt, doc_id=doc_id, pct_cb=pct_cb)

    if job_g.status != "completed" or not (job_g.result_text or "").strip():
        logger.warning(
            f"[wiw] Gemini Flash failed: status={job_g.status} err={job_g.error!r}"
        )
        return GenerationJob(
            job_id=job_g.job_id,
            status="failed",
            error=f"Gemini Flash: {job_g.error or 'empty prompt'}",
            raw={"gemini": job_g.raw},
        )

    mj_prompt = job_g.result_text.strip()
    logger.info(
        f"[wiw] Gemini Flash ready (chars={len(mj_prompt)}); submitting MJ Imagine"
    )
    if progress_cb is not None:
        try:
            await progress_cb("MJ Imagine")
        except Exception as e:
            logger.debug(f"[wiw] progress_cb failed (non-fatal): {e!r}")

    mj_wf = MidJourneyImagineWorkflow(client, aspect_ratio=aspect_ratio)
    mj_wf.on_progress = pct_cb
    job_m = await mj_wf.run(prompt=mj_prompt)

    # Раскладываем mj_task_id и исходный prompt-после-Gemini в raw для scenarios.py
    # (recipe сохранит mj_task_id, чтобы U1-U4/V1-V4 могли указывать на этот grid).
    raw = dict(job_m.raw or {})
    try:
        raw["mj_task_id"] = int(job_m.job_id)
    except (TypeError, ValueError):
        raw["mj_task_id"] = None
    raw["gemini_prompt"] = mj_prompt
    return GenerationJob(
        job_id=job_m.job_id,
        status=job_m.status,
        result_urls=list(job_m.result_urls),
        result_text=job_m.result_text,
        error=job_m.error,
        raw=raw,
    )
