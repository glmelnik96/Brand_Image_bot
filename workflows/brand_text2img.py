"""
Brand-консистентный text→image композер.

Pipeline (Path A — два sequential tasks, как UI Phygital+):
  user_text → Gemini Text (system-prompt-документ Cloud.ru Enhancer) → description
  description → Nano Banana → image

С 2026-05-20 поддерживает три варианта брендового энхансера через `variant`:
  - "photo"     — SYSTEM_PROMPT_Gemini3Pro_CloudRu_Photo_Enhancer.md
  - "render"    — SYSTEM_PROMPT_Gemini3Pro_CloudRu_Render_Enhancer.md
  - "isometric" — SYSTEM_PROMPT_Gemini3Pro_CloudRu_Isometric_Enhancer.md

Safety-retry (2026-05-20): если Nano Banana вернул safety reject c сообщением
«Please remove potential harmful word X from the prompt…», прогоняем промпт
через Gemini-scrubber (отдельный system_prompt-документ), который точечно
вычищает flagged word и его морфо-формы, сохраняя брендовую дисциплину, и
перезапускаем Nano Banana. Делаем до MAX_SCRUB_RETRIES попыток.

Возвращает финальный GenerationJob от Nano Banana. Если Gemini Text упал —
возвращает FAILED job с error="Gemini Text: ..." и raw={"gemini": ...},
чтобы исполнитель в bot/scenarios.py показал понятную ошибку.

Регенерация (🔄): bot заново зовёт эту функцию с тем же user_text + variant →
Gemini Text всё равно вернёт новый description (стохастика), и результат
будет другой — это ожидаемое поведение.
"""

from __future__ import annotations

import re
from typing import Awaitable, Callable, Optional

from loguru import logger

from client.api import PhygitalClient
from client.models import GenerationJob
from workflows.brand_docs import (
    SCRUBBER_DOC,
    VARIANT_DOCS,
    get_scrubber_doc,
    get_text2img_doc,
    invalidate_brand_doc,
)
from workflows.gemini_text import GeminiTextWorkflow
from workflows.image_gen import ImageGenWorkflow

# Callback для статус-апдейтов между шагами цепочки. None — молча работаем.
ProgressCb = Optional[Callable[[str], Awaitable[None]]]

# Максимум попыток sanitize+retry, если Nano Banana подряд режет safety filter.
# 2 — чтобы хватило на цепочку «отклонил A → почистили → отклонил B → почистили»,
# но не утопить юзера в долгом ожидании если scrubber не сходится.
MAX_SCRUB_RETRIES = 2

# Парсер сообщения от Phygital/Nano Banana вида
# «Please remove potential harmful word knob from the prompt and press Generate again»
# Берём первое слово после «word(s)» и до «from».
_SAFETY_PATTERN = re.compile(
    r"remove\s+(?:the\s+)?(?:potential(?:ly)?\s+)?harmful\s+words?\s+"
    r"['\"`]?(?P<word>[^\s,'\"`]+)['\"`]?"
    r"\s+from",
    re.IGNORECASE,
)


def _is_stale_doc_error(job: GenerationJob) -> bool:
    """True, если Gemini Text упал с 'Cannot upload files' — Phygital storage
    выкинул file_obj_id раньше TTL и кэш брендового документа надо переаплоадить."""
    if job.status == "completed":
        return False
    err = (job.error or "").lower()
    return "cannot upload files" in err


def _extract_flagged_word(job: GenerationJob) -> str | None:
    """Если ошибка — safety-reject Nano Banana, вернуть flagged word; иначе None."""
    if job.status == "completed":
        return None
    err = job.error or ""
    m = _SAFETY_PATTERN.search(err)
    if not m:
        return None
    word = m.group("word").strip().strip('".,!?:;\'`')
    return word or None


async def _scrub_prompt(
    client: PhygitalClient,
    *,
    prompt: str,
    flagged_word: str,
) -> str | None:
    """Прогнать промпт через Gemini-scrubber. Возвращает почищенный промпт или
    None, если Gemini Text упал/пустой (тогда retry смысла не имеет)."""
    scrubber_doc_id = await get_scrubber_doc(client)
    scrub_input = (
        f"FLAGGED WORD: {flagged_word}\n\nPROMPT TO FIX:\n{prompt}"
    )
    wf = GeminiTextWorkflow(client)
    job = await wf.run_text(prompt=scrub_input, document_ids=[scrubber_doc_id])
    if _is_stale_doc_error(job):
        logger.warning(
            "[scrubber] Phygital reported stale scrubber doc — invalidating cache and retrying once"
        )
        await invalidate_brand_doc(SCRUBBER_DOC)
        scrubber_doc_id = await get_scrubber_doc(client)
        wf = GeminiTextWorkflow(client)
        job = await wf.run_text(prompt=scrub_input, document_ids=[scrubber_doc_id])
    if job.status != "completed":
        logger.warning(
            f"[scrubber] Gemini Text failed: status={job.status} err={job.error!r}"
        )
        return None
    cleaned = (job.result_text or "").strip()
    if not cleaned:
        logger.warning("[scrubber] Gemini Text returned empty cleaned prompt")
        return None
    return cleaned


async def run_brand_text2img(
    client: PhygitalClient,
    *,
    prompt: str,
    variant: str = "photo",
    model_name: str = "v3_1",
    ratio: str = "default",
    resolution: str = "default",
    progress_cb: ProgressCb = None,
) -> GenerationJob:
    """Bind user-text → описание (Gemini c вариантным system-prompt) → картинка (Nano Banana)."""
    doc_id = await get_text2img_doc(client, variant)
    logger.info(f"[brand_t2i:{variant}] using system-prompt doc_id={doc_id}")
    text_wf = GeminiTextWorkflow(client)
    job_t = await text_wf.run_text(prompt=prompt, document_ids=[doc_id])
    if _is_stale_doc_error(job_t):
        stale_doc = VARIANT_DOCS.get(variant)
        logger.warning(
            f"[brand_t2i:{variant}] Phygital reported stale brand doc "
            f"(was id={doc_id}); invalidating cache and retrying Gemini Text once"
        )
        if stale_doc:
            await invalidate_brand_doc(stale_doc)
        doc_id = await get_text2img_doc(client, variant)
        logger.info(f"[brand_t2i:{variant}] retry with fresh doc_id={doc_id}")
        text_wf = GeminiTextWorkflow(client)
        job_t = await text_wf.run_text(prompt=prompt, document_ids=[doc_id])
    if job_t.status != "completed" or not (job_t.result_text or "").strip():
        logger.warning(
            f"[brand_t2i:{variant}] Gemini Text failed: status={job_t.status} err={job_t.error!r}"
        )
        return GenerationJob(
            job_id=job_t.job_id,
            status="failed",
            error=f"Gemini Text: {job_t.error or 'empty description'}",
            raw={"gemini": job_t.raw},
        )

    description = job_t.result_text
    logger.info(
        f"[brand_t2i:{variant}] Gemini description ready (chars={len(description)}); "
        f"submitting Nano Banana"
    )
    if progress_cb is not None:
        try:
            await progress_cb("Nano Banana")
        except Exception as e:
            logger.debug(f"[brand_t2i:{variant}] progress_cb failed (non-fatal): {e!r}")

    current_prompt = description

    def _new_img_wf() -> ImageGenWorkflow:
        # Каждый retry — свежий ImageGenWorkflow, чтобы _last_price/state
        # не утекали между сабмитами.
        return ImageGenWorkflow(
            client, model_name=model_name, ratio=ratio, resolution=resolution
        )

    img_job = await _new_img_wf().run(prompt=current_prompt)

    for attempt in range(1, MAX_SCRUB_RETRIES + 1):
        flagged = _extract_flagged_word(img_job)
        if not flagged:
            # либо успех, либо какая-то другая (не-safety) ошибка — не лезем чинить
            break
        logger.info(
            f"[brand_t2i:{variant}] Nano Banana safety-reject "
            f"(attempt={attempt}/{MAX_SCRUB_RETRIES}) flagged_word={flagged!r}; "
            f"running Gemini scrubber"
        )
        if progress_cb is not None:
            try:
                await progress_cb(
                    f"Чистка safety-words: «{flagged}» (попытка {attempt})"
                )
            except Exception as e:
                logger.debug(
                    f"[brand_t2i:{variant}] progress_cb failed (non-fatal): {e!r}"
                )
        cleaned = await _scrub_prompt(client, prompt=current_prompt, flagged_word=flagged)
        if not cleaned:
            logger.warning(
                f"[brand_t2i:{variant}] scrubber returned nothing — aborting retry loop"
            )
            break
        if cleaned == current_prompt:
            logger.warning(
                f"[brand_t2i:{variant}] scrubber returned identical prompt — aborting"
            )
            break
        current_prompt = cleaned
        logger.info(
            f"[brand_t2i:{variant}] scrubbed prompt ready (chars={len(current_prompt)}); "
            f"resubmitting Nano Banana"
        )
        if progress_cb is not None:
            try:
                await progress_cb("Nano Banana (повтор)")
            except Exception as e:
                logger.debug(
                    f"[brand_t2i:{variant}] progress_cb failed (non-fatal): {e!r}"
                )
        img_job = await _new_img_wf().run(prompt=current_prompt)

    return img_job
