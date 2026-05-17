"""
Brand-консистентный text→image композер.

Pipeline (Path A — два sequential tasks, как UI Phygital+):
  user_text → Gemini Text (system-prompt-документ Cloud.ru Enhancer) → description
  description → Nano Banana → image

Возвращает финальный GenerationJob от Nano Banana. Если Gemini Text упал —
возвращает FAILED job с error="Gemini Text: ..." и raw={"gemini": ...},
чтобы исполнитель в bot/scenarios.py показал понятную ошибку.

Регенерация (🔄): bot заново зовёт эту функцию с тем же user_text →
Gemini Text всё равно вернёт новый description (стохастика), и результат
будет другой — это ожидаемое поведение.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

from loguru import logger

from client.api import PhygitalClient
from client.models import GenerationJob
from workflows.brand_docs import get_text2img_doc
from workflows.gemini_text import GeminiTextWorkflow
from workflows.image_gen import ImageGenWorkflow

# Callback для статус-апдейтов между шагами цепочки. None — молча работаем.
ProgressCb = Optional[Callable[[str], Awaitable[None]]]


async def run_brand_text2img(
    client: PhygitalClient,
    *,
    prompt: str,
    model_name: str = "v3_1",
    ratio: str = "default",
    resolution: str = "default",
    progress_cb: ProgressCb = None,
) -> GenerationJob:
    """Bind user-text → описание (Gemini) → картинка (Nano Banana)."""
    doc_id = await get_text2img_doc(client)
    text_wf = GeminiTextWorkflow(client)
    job_t = await text_wf.run_text(prompt=prompt, document_ids=[doc_id])
    if job_t.status != "completed" or not (job_t.result_text or "").strip():
        logger.warning(
            f"[brand_t2i] Gemini Text failed: status={job_t.status} err={job_t.error!r}"
        )
        return GenerationJob(
            job_id=job_t.job_id,
            status="failed",
            error=f"Gemini Text: {job_t.error or 'empty description'}",
            raw={"gemini": job_t.raw},
        )

    description = job_t.result_text
    logger.info(
        f"[brand_t2i] Gemini description ready (chars={len(description)}); "
        f"submitting Nano Banana"
    )
    if progress_cb is not None:
        try:
            await progress_cb("Gemini готов, запускаю Nano Banana…")
        except Exception as e:
            logger.debug(f"[brand_t2i] progress_cb failed (non-fatal): {e!r}")
    img_wf = ImageGenWorkflow(
        client, model_name=model_name, ratio=ratio, resolution=resolution
    )
    return await img_wf.run(prompt=description)
