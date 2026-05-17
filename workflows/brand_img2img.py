"""
Brand-консистентный image→image композер.

Pipeline (Path A):
  user image(s) → Gemini Text (фикс. prompt + system-prompt-документ
                                Cloud.ru Img2Img Enhancer + те же изображения) → description
  same images + description → Nano Banana → image

Аплоадим картинки ОДИН раз через ImageToImageWorkflow.upload_images и
переиспользуем file_obj_id между Gemini Text и Nano Banana submits.

text_prompt для Gemini зафиксирован: "Read the System Prompt". Вся брендовая
логика — внутри system-prompt-документа.

Регенерация: bot заново зовёт run_brand_img2img с теми же init-файлами →
Gemini вернёт новый description → новая картинка от Nano Banana.
"""

from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable, Optional

from loguru import logger

from client.api import PhygitalClient
from client.models import GenerationJob
from workflows.brand_docs import get_img2img_doc
from workflows.gemini_text import GeminiTextWorkflow
from workflows.image_to_image import ImageToImageWorkflow

# Фикс-текст для Gemini Text. Не редактируется пользователем — суть в system-prompt-документе.
BRAND_I2I_PROMPT = "Read the System Prompt"
# Callback для статус-апдейтов между шагами цепочки. None — молча работаем.
ProgressCb = Optional[Callable[[str], Awaitable[None]]]


async def run_brand_img2img(
    client: PhygitalClient,
    *,
    init_paths: list[str | Path],
    model_name: str = "v3_1",
    ratio: str = "r_3_4",
    resolution: str = "k2",
    progress_cb: ProgressCb = None,
) -> GenerationJob:
    """Bind user-image(s) → описание (Gemini) → картинка (Nano Banana с теми же init-img)."""
    img_wf = ImageToImageWorkflow(
        client, model_name=model_name, ratio=ratio, resolution=resolution
    )
    # Аплоадим один раз; ids/dims переиспользуем для Gemini Text и Nano Banana.
    ids, dims = await img_wf.upload_images(init_paths)
    img_wf._init_img_ids = ids
    img_wf._init_img_dims = dims

    doc_id = await get_img2img_doc(client)
    text_wf = GeminiTextWorkflow(client)
    job_t = await text_wf.run_text(
        prompt=BRAND_I2I_PROMPT,
        init_img_ids=ids,
        init_img_dims=dims,
        document_ids=[doc_id],
    )
    if job_t.status != "completed" or not (job_t.result_text or "").strip():
        logger.warning(
            f"[brand_i2i] Gemini Text failed: status={job_t.status} err={job_t.error!r}"
        )
        return GenerationJob(
            job_id=job_t.job_id,
            status="failed",
            error=f"Gemini Text: {job_t.error or 'empty description'}",
            raw={"gemini": job_t.raw},
        )

    description = job_t.result_text
    logger.info(
        f"[brand_i2i] Gemini description ready (chars={len(description)}); "
        f"submitting Nano Banana with same {len(ids)} init image(s)"
    )
    if progress_cb is not None:
        try:
            await progress_cb("Gemini готов, запускаю Nano Banana…")
        except Exception as e:
            logger.debug(f"[brand_i2i] progress_cb failed (non-fatal): {e!r}")
    # img_wf уже держит _init_img_ids/_init_img_dims — build_payload подставит их в init_img.
    return await img_wf.run(prompt=description)
