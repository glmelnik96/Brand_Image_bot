"""B2B-канал для сервисных запросов от других Telegram-ботов.

Зачем: соседний motion-бот (@Resizecloudru_bot) должен делегировать генерацию
hero-картинки нашему боту вместо ручной загрузки. Bot API 10.0 (2026-05) разрешает
боту слать сообщения другому боту, если оба включили Bot-to-Bot Communication
Mode в BotFather.

Wire protocol — обычный текст. Первая строка — заголовок, дальше пустая строка,
дальше тело prompt:

    @b2b <variant> <ratio> <resolution> corr=<4..16hex>

    <многострочный prompt-текст>

Поля заголовка:
  variant     — render | photo | isometric
  ratio       — "W:H" (человекочитаемо) ИЛИ "r_W_H" (Phygital-enum).
                Допустимые значения: 1:1, 3:4, 4:3, 9:16, 16:9.
                Обе формы валидны и эквивалентны; нормализация в r_W_H
                делается приёмной стороной. Невалидное значение →
                reason=bad_ratio (мгновенно, до submit'а).
  resolution  — k1 | k2 | k3 (Phygital-enum, пробрасывается as-is).
  corr        — корреляционный id sender'а (4–16 hex), эхо-вернётся в ответе.

Успех: reply_photo(<bytes>, caption="@b2b OK corr=<corr>").
Ошибка: reply_text("@b2b ERROR corr=<corr> reason=<code>"), где code:
    bad_header, bad_ratio, empty_prompt, not_whitelisted (молча),
    gemini_failed, safety_blocked, no_result, download_failed, timeout,
    internal.

Не делаем:
  - Никаких StatusReporter-апдейтов (это машинный канал, любой шум сломает парсер).
  - Никаких recipe / БД.
  - Никакого setMyCommands.
  - В TG прилетает только OK/ERROR; prompt body не эхо-вернётся.
"""
from __future__ import annotations

import asyncio
import re

import httpx
from loguru import logger
from telegram import Update
from telegram.ext import (
    ApplicationHandlerStop,
    ContextTypes,
    MessageHandler,
    filters,
)

from client.api import _SSL_CTX, PhygitalClient
from client.config import settings
from workflows.brand_text2img import _extract_flagged_word, run_brand_text2img

# Заголовок: @b2b <variant> <ratio> <resolution> corr=<hex>.
# variant — одно из render/photo/isometric.
# ratio/resolution — произвольные не-пробельные токены, пробрасываются в workflow.
B2B_HEADER_RE = re.compile(
    r"^@b2b\s+(?P<variant>render|photo|isometric)"
    r"\s+(?P<ratio>\S+)"
    r"\s+(?P<resolution>\S+)"
    r"\s+corr=(?P<corr>[a-f0-9]{4,16})\s*$"
)

# Phygital требует ratio как enum r_W_H. Sender-боты могут слать "3:4" или уже
# "r_3_4" — поддерживаем оба, иначе validator на сервере nullит inputs/outputs и
# таск висит pending до queue-TTL-cancel (см. инцидент 2026-06-07).
_RATIO_ALLOWED = {"r_1_1", "r_3_4", "r_4_3", "r_9_16", "r_16_9"}
_RATIO_HUMAN_RE = re.compile(r"^(\d+):(\d+)$")


def _normalize_ratio(raw: str) -> str | None:
    """W:H или r_W_H → канонический Phygital-enum. None, если не распознан."""
    if raw in _RATIO_ALLOWED:
        return raw
    m = _RATIO_HUMAN_RE.match(raw)
    if m:
        candidate = f"r_{m.group(1)}_{m.group(2)}"
        if candidate in _RATIO_ALLOWED:
            return candidate
    return None

# Семафор: ограничиваем параллельные b2b-задачи (отдельно от user-семафора).
_b2b_sem: asyncio.Semaphore | None = None


def _get_b2b_sem() -> asyncio.Semaphore:
    """Лениво создаём семафор внутри event loop (не на import-time)."""
    global _b2b_sem
    if _b2b_sem is None:
        _b2b_sem = asyncio.Semaphore(max(1, settings.b2b_max_concurrency))
    return _b2b_sem


async def _download_image_bytes(url: str, *, timeout: float = 120.0) -> bytes:
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, verify=_SSL_CTX
    ) as cli:
        r = await cli.get(url)
        r.raise_for_status()
        return r.content


async def _run_one_b2b_job(
    *, prompt: str, variant: str, ratio: str, resolution: str, corr: str,
) -> tuple[bytes | None, str | None]:
    """Гоняет цепочку Gemini→Nano Banana через PhygitalClient и качает результат.

    Возвращает (image_bytes, error_code). Если image_bytes is not None — успех.
    error_code — один из: gemini_failed / safety_blocked / no_result /
    download_failed / internal.
    """
    # Лениво подтягиваем state — чтобы не было циклического импорта на import-time.
    from bot.state import get_state

    state = get_state()
    log = logger.bind(b2b=True, corr=corr, variant=variant)
    try:
        async with PhygitalClient(
            state.session, session_manager=state.session_manager,
        ) as client:
            job = await run_brand_text2img(
                client,
                prompt=prompt,
                variant=variant,
                model_name="v3_1",
                ratio=ratio,
                resolution=resolution,
                progress_cb=None,
                pct_cb=None,
            )
    except Exception as e:
        log.opt(exception=e).error(f"b2b: run_brand_text2img crashed: {e!r}")
        return None, "internal"

    if job.status != "completed":
        # Различаем safety vs прочие ошибки.
        if _extract_flagged_word(job) is not None:
            log.warning(f"b2b: safety_blocked after retries, err={job.error!r}")
            return None, "safety_blocked"
        err_lower = (job.error or "").lower()
        if "gemini text" in err_lower or "gemini" in err_lower:
            log.warning(f"b2b: gemini_failed err={job.error!r}")
            return None, "gemini_failed"
        log.warning(f"b2b: workflow failed status={job.status} err={job.error!r}")
        return None, "internal"

    if not job.result_urls:
        log.warning("b2b: completed but result_urls empty")
        return None, "no_result"

    url = job.result_urls[0]
    try:
        data = await _download_image_bytes(url)
    except Exception as e:
        log.opt(exception=e).warning(f"b2b: download failed: {e!r}")
        return None, "download_failed"
    log.info(f"b2b: success, bytes={len(data)} url={url[:80]}…")
    return data, None


async def b2b_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """MessageHandler для b2b-канала. Регистрируется на group=-1."""
    msg = update.effective_message
    if msg is None or not msg.text:
        return
    sender = msg.from_user
    # Только bot-отправитель.
    if not sender or not sender.is_bot:
        return
    sender_uname = (sender.username or "").lower()
    whitelist = settings.b2b_whitelist
    if sender_uname not in whitelist:
        # Молча — это не наш партнёр.
        logger.bind(b2b=True).debug(
            f"b2b: ignored non-whitelisted bot @{sender_uname or '?'}"
        )
        return

    # Парс header / body.
    lines = msg.text.split("\n", 1)
    header = lines[0]
    body = lines[1].lstrip("\n") if len(lines) > 1 else ""

    m = B2B_HEADER_RE.match(header)
    if not m:
        # corr ещё не знаем — отдаём unknown.
        logger.bind(b2b=True).info(
            f"b2b: bad_header from @{sender_uname} header={header[:120]!r}"
        )
        await msg.reply_text("@b2b ERROR corr=unknown reason=bad_header")
        raise ApplicationHandlerStop

    corr = m.group("corr")
    variant = m.group("variant")
    raw_ratio = m.group("ratio")
    resolution = m.group("resolution")
    log = logger.bind(b2b=True, corr=corr, variant=variant, sender=sender_uname)

    ratio = _normalize_ratio(raw_ratio)
    if ratio is None:
        log.info(f"b2b: bad_ratio raw={raw_ratio!r}")
        await msg.reply_text(f"@b2b ERROR corr={corr} reason=bad_ratio")
        raise ApplicationHandlerStop
    if ratio != raw_ratio:
        log.debug(f"b2b: ratio normalized {raw_ratio!r} -> {ratio!r}")

    if not body.strip():
        log.info("b2b: empty_prompt")
        await msg.reply_text(f"@b2b ERROR corr={corr} reason=empty_prompt")
        raise ApplicationHandlerStop

    log.info(
        f"b2b: accepted variant={variant} ratio={ratio} resolution={resolution} "
        f"body_len={len(body)}"
    )

    # Не светим prompt в TG. Только в логи (если уровень DEBUG/TRACE).
    log.debug(f"b2b: body preview={body[:200]!r}")

    timeout_sec = settings.b2b_request_timeout_sec
    try:
        async with _get_b2b_sem():
            image_bytes, err = await asyncio.wait_for(
                _run_one_b2b_job(
                    prompt=body, variant=variant,
                    ratio=ratio, resolution=resolution, corr=corr,
                ),
                timeout=timeout_sec,
            )
    except asyncio.TimeoutError:
        log.warning(f"b2b: timeout after {timeout_sec}s")
        try:
            await msg.reply_text(f"@b2b ERROR corr={corr} reason=timeout")
        except Exception as e:
            log.opt(exception=e).warning(f"b2b: reply on timeout failed: {e!r}")
        raise ApplicationHandlerStop
    except Exception as e:
        log.opt(exception=e).error(f"b2b: unexpected crash: {e!r}")
        try:
            await msg.reply_text(f"@b2b ERROR corr={corr} reason=internal")
        except Exception:
            pass
        raise ApplicationHandlerStop

    if image_bytes is None:
        await msg.reply_text(f"@b2b ERROR corr={corr} reason={err or 'internal'}")
        raise ApplicationHandlerStop

    try:
        await msg.reply_photo(photo=image_bytes, caption=f"@b2b OK corr={corr}")
        log.info("b2b: OK photo sent")
    except Exception as e:
        log.opt(exception=e).error(f"b2b: reply_photo failed: {e!r}")
        try:
            await msg.reply_text(f"@b2b ERROR corr={corr} reason=internal")
        except Exception:
            pass
    raise ApplicationHandlerStop


def build_b2b_handler() -> MessageHandler:
    """Возвращает готовый MessageHandler для регистрации в Application.

    Фильтр: только TEXT, регулярка ^@b2b  — так мы не дёрнем conversation,
    если префикса нет.
    """
    return MessageHandler(filters.TEXT & filters.Regex(r"^@b2b "), b2b_handler)
