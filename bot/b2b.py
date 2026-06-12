"""B2B-канал для сервисных запросов от других Telegram-ботов.

Зачем: соседний motion-бот (@Resizecloudru_bot) должен делегировать генерацию
hero-картинки нашему боту вместо ручной загрузки. Bot API 10.0 (2026-05) разрешает
боту слать сообщения другому боту, если оба включили Bot-to-Bot Communication
Mode в BotFather.

────────────────────────────────────────────────────────────────────────────
Команда 1 — ГЕНЕРАЦИЯ (text2img). Wire protocol — обычный текст. Первая строка —
заголовок, дальше пустая строка, дальше тело prompt:

    @b2b <variant> <ratio> <resolution> [rmbg=1] corr=<4..16hex>

    <многострочный prompt-текст>

Поля заголовка:
  variant     — render | photo | isometric
  ratio       — "W:H" (человекочитаемо) ИЛИ "r_W_H" (Phygital-enum).
                Допустимые значения: 1:1, 3:4, 4:3, 9:16, 16:9.
                Обе формы валидны и эквивалентны; нормализация в r_W_H
                делается приёмной стороной. Невалидное значение →
                reason=bad_ratio (мгновенно, до submit'а).
  resolution  — k1 | k2 | k3 (Phygital-enum, пробрасывается as-is).
  rmbg        — опционально. rmbg=1 → после генерации СРАЗУ прогоняем результат
                через Photoroom (workflow 125) ещё до отдачи и возвращаем PNG с
                alpha как Document. Исходник в этот момент не пережат TG → маска
                максимального качества, минус раунд-трип. Требует включённого
                B2B_REMOVEBG_ENABLED, иначе reason=removebg_disabled.
  corr        — корреляционный id sender'а (4–16 hex), эхо-вернётся в ответе.

Успех (без rmbg): reply_photo(<bytes>, caption="@b2b OK corr=<corr>").
Успех (rmbg=1):    reply_document(<PNG+alpha>, caption="@b2b OK corr=<corr>").

────────────────────────────────────────────────────────────────────────────
Команда 2 — REMOVE BACKGROUND (отдельный сценарий для пользовательских загрузок).
Картинка + команда в ОДНОМ сообщении: caption у photo/document.

    caption: @b2b removebg corr=<4..16hex>
    attachment: исходное изображение (лучше как Document, чтобы TG не пережал)

Вход: photo или document, ≤ B2B_REMOVEBG_MAX_INPUT_BYTES (12 MB). >2048px ужмётся.
Успех: reply_document(<PNG+alpha>, caption="@b2b OK corr=<corr>") — Document,
       иначе TG пережмёт в JPEG и убьёт alpha.
Требует B2B_REMOVEBG_ENABLED=true, иначе reason=removebg_disabled.

────────────────────────────────────────────────────────────────────────────
Ошибка (оба пути): reply_text("@b2b ERROR corr=<corr> reason=<code>"), где code:
    bad_header, bad_ratio, empty_prompt, not_whitelisted (молча),
    gemini_failed, safety_blocked, no_result, download_failed, timeout,
    internal,                                  # — генерация
    removebg_disabled, no_image, bad_input, input_too_large, photoroom_failed.
                                               # — removebg / chained rmbg

Не делаем:
  - Никаких StatusReporter-апдейтов (это машинный канал, любой шум сломает парсер).
  - Никаких recipe / БД.
  - Никакого setMyCommands.
  - В TG прилетает только OK/ERROR; prompt body не эхо-вернётся.
"""
from __future__ import annotations

import asyncio
import io
import re
import shutil
import tempfile
import time
from pathlib import Path

import httpx
from loguru import logger
from telegram import InputFile, Update
from telegram.ext import (
    ApplicationHandlerStop,
    ContextTypes,
    MessageHandler,
    filters,
)

from client.api import _SSL_CTX, PhygitalClient
from client.config import settings
from workflows.brand_text2img import _extract_flagged_word, run_brand_text2img
from workflows.photoroom import PhotoroomBgRemoveWorkflow

# Заголовок генерации: @b2b <variant> <ratio> <resolution> [rmbg=1] corr=<hex>.
# variant — одно из render/photo/isometric.
# ratio/resolution — произвольные не-пробельные токены, пробрасываются в workflow.
# rmbg — опциональный флаг 0/1: chained background removal после генерации.
B2B_GEN_HEADER_RE = re.compile(
    r"^@b2b\s+(?P<variant>render|photo|isometric)"
    r"\s+(?P<ratio>\S+)"
    r"\s+(?P<resolution>\S+)"
    r"(?:\s+rmbg=(?P<rmbg>[01]))?"
    r"\s+corr=(?P<corr>[a-f0-9]{4,16})\s*$"
)

# Заголовок removebg: @b2b removebg corr=<hex>. Без ratio/resolution/prompt —
# отдельная команда, а не variant (см. спеку): у вырезки фона нет этих полей.
B2B_REMOVEBG_RE = re.compile(
    r"^@b2b\s+removebg\s+corr=(?P<corr>[a-f0-9]{4,16})\s*$"
)

# Phygital требует ratio как enum r_W_H. Sender-боты могут слать "3:4" или уже
# "r_3_4" — поддерживаем оба, иначе validator на сервере nullит inputs/outputs и
# таск висит pending до queue-TTL-cancel (см. инцидент 2026-06-07).
_RATIO_ALLOWED = {"r_1_1", "r_3_4", "r_4_3", "r_9_16", "r_16_9"}
_RATIO_HUMAN_RE = re.compile(r"^(\d+):(\d+)$")

# Жёсткий потолок входного файла для removebg (TG getFile тянет до 20 MB).
B2B_REMOVEBG_MAX_INPUT_BYTES = 12 * 1024 * 1024


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


# Два независимых семафора: генерация (тяжёлая, Gemini→Nano Banana) и removebg
# (лёгкая Photoroom-нода). Лениво создаём внутри event loop, не на import-time.
_b2b_sem: asyncio.Semaphore | None = None
_b2b_removebg_sem: asyncio.Semaphore | None = None


def _get_b2b_sem() -> asyncio.Semaphore:
    global _b2b_sem
    if _b2b_sem is None:
        _b2b_sem = asyncio.Semaphore(max(1, settings.b2b_max_concurrency))
    return _b2b_sem


def _get_removebg_sem() -> asyncio.Semaphore:
    global _b2b_removebg_sem
    if _b2b_removebg_sem is None:
        _b2b_removebg_sem = asyncio.Semaphore(
            max(1, settings.b2b_removebg_max_concurrency)
        )
    return _b2b_removebg_sem


async def _download_image_bytes(url: str, *, timeout: float = 120.0) -> bytes:
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, verify=_SSL_CTX
    ) as cli:
        r = await cli.get(url)
        r.raise_for_status()
        return r.content


async def _run_photoroom(
    client: PhygitalClient, *, init_path: Path, corr: str, log,
) -> tuple[bytes | None, str | None]:
    """Гоняет Photoroom-ноду (workflow 125) на одной картинке и качает результат.

    Возвращает (png_bytes, error_code). png_bytes is not None → успех (PNG с alpha).
    error_code ∈ photoroom_failed / download_failed.
    """
    try:
        wf = PhotoroomBgRemoveWorkflow(client)
        job = await wf.run_with_file(init_path=init_path)
    except Exception as e:
        log.opt(exception=e).error(f"b2b: photoroom crashed: {e!r}")
        return None, "photoroom_failed"

    if job.status != "completed" or not job.result_urls:
        log.warning(
            f"b2b: photoroom failed status={job.status} err={job.error!r}"
        )
        return None, "photoroom_failed"

    try:
        data = await _download_image_bytes(job.result_urls[0])
    except Exception as e:
        log.opt(exception=e).warning(f"b2b: photoroom download failed: {e!r}")
        return None, "download_failed"
    log.info(f"b2b: photoroom OK, png_bytes={len(data)}")
    return data, None


async def _run_one_b2b_job(
    *, prompt: str, variant: str, ratio: str, resolution: str, corr: str,
    rmbg: bool = False,
) -> tuple[bytes | None, bool, str | None]:
    """Гоняет цепочку Gemini→Nano Banana, опционально chained Photoroom.

    Возвращает (image_bytes, is_document, error_code).
      - is_document=True → результат это PNG с alpha (chained rmbg), слать Document.
      - error_code — gemini_failed / safety_blocked / no_result / download_failed /
        photoroom_failed / internal.
    """
    # Лениво подтягиваем state — чтобы не было циклического импорта на import-time.
    from bot.state import get_state

    state = get_state()
    log = logger.bind(b2b=True, corr=corr, variant=variant)
    tmp_path: Path | None = None
    try:
        async with PhygitalClient(
            state.session, session_manager=state.session_manager,
        ) as client:
            try:
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
                return None, False, "internal"

            if job.status != "completed":
                # Различаем safety vs прочие ошибки.
                if _extract_flagged_word(job) is not None:
                    log.warning(f"b2b: safety_blocked after retries, err={job.error!r}")
                    return None, False, "safety_blocked"
                err_lower = (job.error or "").lower()
                if "gemini text" in err_lower or "gemini" in err_lower:
                    log.warning(f"b2b: gemini_failed err={job.error!r}")
                    return None, False, "gemini_failed"
                log.warning(f"b2b: workflow failed status={job.status} err={job.error!r}")
                return None, False, "internal"

            if not job.result_urls:
                log.warning("b2b: completed but result_urls empty")
                return None, False, "no_result"

            gen_url = job.result_urls[0]
            try:
                gen_bytes = await _download_image_bytes(gen_url)
            except Exception as e:
                log.opt(exception=e).warning(f"b2b: download failed: {e!r}")
                return None, False, "download_failed"
            log.info(f"b2b: gen success, bytes={len(gen_bytes)} url={gen_url[:80]}…")

            if not rmbg:
                return gen_bytes, False, None

            # Chained: пишем pristine-результат во временный файл и сразу через
            # Photoroom. Исходник не пережат TG → маска максимального качества.
            tmp_dir = Path(tempfile.mkdtemp(prefix="b2b-chain-"))
            tmp_path = tmp_dir / f"gen_{corr}.png"
            tmp_path.write_bytes(gen_bytes)
            png_bytes, err = await _run_photoroom(
                client, init_path=tmp_path, corr=corr, log=log,
            )
            if png_bytes is None:
                return None, False, err
            return png_bytes, True, None
    except Exception as e:
        log.opt(exception=e).error(f"b2b: job crashed: {e!r}")
        return None, False, "internal"
    finally:
        if tmp_path is not None:
            shutil.rmtree(tmp_path.parent, ignore_errors=True)


async def _run_one_removebg_job(
    *, init_path: Path, corr: str,
) -> tuple[bytes | None, str | None]:
    """Standalone removebg: одна загруженная картинка → Photoroom → PNG с alpha."""
    from bot.state import get_state

    state = get_state()
    log = logger.bind(b2b=True, corr=corr, op="removebg")
    try:
        async with PhygitalClient(
            state.session, session_manager=state.session_manager,
        ) as client:
            return await _run_photoroom(client, init_path=init_path, corr=corr, log=log)
    except Exception as e:
        log.opt(exception=e).error(f"b2b: removebg crashed: {e!r}")
        return None, "internal"


async def _download_tg_attachment(
    bot, msg, dest: Path,
) -> tuple[Path | None, str | None]:
    """Качает вложение (photo или document) во временный файл dest-каталога.

    Возвращает (path, error_code). error_code ∈ no_image / input_too_large / bad_input.
    """
    if msg.document is not None:
        fsize = msg.document.file_size
        if fsize and fsize > B2B_REMOVEBG_MAX_INPUT_BYTES:
            return None, "input_too_large"
        file_id = msg.document.file_id
        suffix = Path(msg.document.file_name or "input.bin").suffix or ".bin"
    elif msg.photo:
        # последний элемент — наибольшее разрешение
        ph = msg.photo[-1]
        fsize = ph.file_size
        if fsize and fsize > B2B_REMOVEBG_MAX_INPUT_BYTES:
            return None, "input_too_large"
        file_id = ph.file_id
        suffix = ".jpg"
    else:
        return None, "no_image"

    try:
        tg_file = await bot.get_file(file_id)
        path = dest / f"b2b_rmbg_{int(time.time() * 1000)}{suffix}"
        await tg_file.download_to_drive(custom_path=str(path))
        return path, None
    except Exception as e:
        logger.bind(b2b=True).warning(f"b2b: attachment download failed: {e!r}")
        return None, "bad_input"


async def _reply_document(msg, data: bytes, corr: str, log) -> None:
    """Отдаём PNG с alpha как Document (TG не пережмёт в JPEG)."""
    try:
        await msg.reply_document(
            document=InputFile(io.BytesIO(data), filename=f"removebg_{corr}.png"),
            caption=f"@b2b OK corr={corr}",
        )
        log.info("b2b: OK document sent")
    except Exception as e:
        log.opt(exception=e).error(f"b2b: reply_document failed: {e!r}")
        try:
            await msg.reply_text(f"@b2b ERROR corr={corr} reason=internal")
        except Exception:
            pass


async def _handle_removebg(
    update: Update, context: ContextTypes.DEFAULT_TYPE, msg, corr: str,
) -> None:
    """Команда @b2b removebg <attachment>. Скачиваем вход → Photoroom → Document."""
    log = logger.bind(b2b=True, corr=corr, op="removebg")
    if not settings.b2b_removebg_enabled:
        log.info("b2b: removebg disabled by flag")
        await msg.reply_text(f"@b2b ERROR corr={corr} reason=removebg_disabled")
        return

    tmp_dir = Path(tempfile.mkdtemp(prefix="b2b-rmbg-"))
    try:
        src, err = await _download_tg_attachment(context.bot, msg, tmp_dir)
        if src is None:
            log.info(f"b2b: removebg input error reason={err}")
            await msg.reply_text(f"@b2b ERROR corr={corr} reason={err}")
            return

        log.info(f"b2b: removebg accepted src={src.name}")
        timeout_sec = settings.b2b_removebg_timeout_sec
        try:
            async with _get_removebg_sem():
                data, perr = await asyncio.wait_for(
                    _run_one_removebg_job(init_path=src, corr=corr),
                    timeout=timeout_sec,
                )
        except asyncio.TimeoutError:
            log.warning(f"b2b: removebg timeout after {timeout_sec}s")
            try:
                await msg.reply_text(f"@b2b ERROR corr={corr} reason=timeout")
            except Exception as e:
                log.opt(exception=e).warning(f"b2b: reply on timeout failed: {e!r}")
            return
        except Exception as e:
            log.opt(exception=e).error(f"b2b: removebg unexpected crash: {e!r}")
            try:
                await msg.reply_text(f"@b2b ERROR corr={corr} reason=internal")
            except Exception:
                pass
            return

        if data is None:
            await msg.reply_text(f"@b2b ERROR corr={corr} reason={perr or 'internal'}")
            return
        await _reply_document(msg, data, corr, log)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def _handle_generation(
    update: Update, context: ContextTypes.DEFAULT_TYPE, msg, m, body: str,
) -> None:
    """Команда генерации @b2b <variant> <ratio> <resolution> [rmbg=1]."""
    corr = m.group("corr")
    variant = m.group("variant")
    raw_ratio = m.group("ratio")
    resolution = m.group("resolution")
    rmbg = m.group("rmbg") == "1"
    log = logger.bind(b2b=True, corr=corr, variant=variant)

    ratio = _normalize_ratio(raw_ratio)
    if ratio is None:
        log.info(f"b2b: bad_ratio raw={raw_ratio!r}")
        await msg.reply_text(f"@b2b ERROR corr={corr} reason=bad_ratio")
        return
    if ratio != raw_ratio:
        log.debug(f"b2b: ratio normalized {raw_ratio!r} -> {ratio!r}")

    if rmbg and not settings.b2b_removebg_enabled:
        log.info("b2b: chained rmbg requested but removebg disabled by flag")
        await msg.reply_text(f"@b2b ERROR corr={corr} reason=removebg_disabled")
        return

    if not body.strip():
        log.info("b2b: empty_prompt")
        await msg.reply_text(f"@b2b ERROR corr={corr} reason=empty_prompt")
        return

    log.info(
        f"b2b: accepted variant={variant} ratio={ratio} resolution={resolution} "
        f"rmbg={int(rmbg)} body_len={len(body)}"
    )
    # Не светим prompt в TG. Только в логи (если уровень DEBUG/TRACE).
    log.debug(f"b2b: body preview={body[:200]!r}")

    # Chained путь дольше: генерация + removebg поверх. Складываем оба таймаута.
    timeout_sec = settings.b2b_request_timeout_sec
    if rmbg:
        timeout_sec += settings.b2b_removebg_timeout_sec
    try:
        async with _get_b2b_sem():
            image_bytes, is_doc, err = await asyncio.wait_for(
                _run_one_b2b_job(
                    prompt=body, variant=variant,
                    ratio=ratio, resolution=resolution, corr=corr, rmbg=rmbg,
                ),
                timeout=timeout_sec,
            )
    except asyncio.TimeoutError:
        log.warning(f"b2b: timeout after {timeout_sec}s")
        try:
            await msg.reply_text(f"@b2b ERROR corr={corr} reason=timeout")
        except Exception as e:
            log.opt(exception=e).warning(f"b2b: reply on timeout failed: {e!r}")
        return
    except Exception as e:
        log.opt(exception=e).error(f"b2b: unexpected crash: {e!r}")
        try:
            await msg.reply_text(f"@b2b ERROR corr={corr} reason=internal")
        except Exception:
            pass
        return

    if image_bytes is None:
        await msg.reply_text(f"@b2b ERROR corr={corr} reason={err or 'internal'}")
        return

    if is_doc:
        # chained rmbg → PNG с alpha, шлём Document.
        await _reply_document(msg, image_bytes, corr, log)
        return
    try:
        await msg.reply_photo(photo=image_bytes, caption=f"@b2b OK corr={corr}")
        log.info("b2b: OK photo sent")
    except Exception as e:
        log.opt(exception=e).error(f"b2b: reply_photo failed: {e!r}")
        try:
            await msg.reply_text(f"@b2b ERROR corr={corr} reason=internal")
        except Exception:
            pass


async def b2b_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """MessageHandler для b2b-канала. Регистрируется на group=-1.

    Диспетчер по первой строке текста/caption:
      - @b2b removebg ...                      → _handle_removebg (нужно вложение)
      - @b2b <variant> <ratio> <res> [rmbg=1]  → _handle_generation
    """
    msg = update.effective_message
    if msg is None:
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

    # Команда живёт либо в text (генерация), либо в caption (removebg + вложение).
    text = msg.text or msg.caption or ""
    parts = text.split("\n", 1)
    header = parts[0]
    body = parts[1].lstrip("\n") if len(parts) > 1 else ""

    # 1) removebg — отдельная грамматика, проверяем первой.
    rm = B2B_REMOVEBG_RE.match(header)
    if rm:
        await _handle_removebg(update, context, msg, rm.group("corr"))
        raise ApplicationHandlerStop

    # 2) генерация.
    gm = B2B_GEN_HEADER_RE.match(header)
    if not gm:
        # corr ещё не знаем — отдаём unknown.
        logger.bind(b2b=True).info(
            f"b2b: bad_header from @{sender_uname} header={header[:120]!r}"
        )
        await msg.reply_text("@b2b ERROR corr=unknown reason=bad_header")
        raise ApplicationHandlerStop

    await _handle_generation(update, context, msg, gm, body)
    raise ApplicationHandlerStop


def build_b2b_handler() -> MessageHandler:
    """Возвращает готовый MessageHandler для регистрации в Application.

    Фильтр ловит два вида сообщений с префиксом @b2b:
      - текст (генерация): TEXT & Regex(^@b2b )
      - вложение+caption (removebg): (PHOTO|Document) & CaptionRegex(^@b2b )
    Так мы не дёрнем conversation, если префикса нет, и видим removebg-картинки.
    """
    text_cmd = filters.TEXT & filters.Regex(r"^@b2b ")
    media_cmd = (filters.PHOTO | filters.Document.ALL) & filters.CaptionRegex(
        r"^@b2b "
    )
    return MessageHandler(text_cmd | media_cmd, b2b_handler)
