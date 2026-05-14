"""Entry point Telegram-бота.

Поддерживает:
- HTTP-прокси для api.telegram.org (Cloud.ru kwts MITM) + кастомный CA pem.
- Whitelist по user_id (`TELEGRAM_ALLOWED_USER_IDS`).
- Три сценария (`/generate`, `/img2img`, `/prep_speaker`) через ConversationHandler.
- Глобальный лимит одновременных задач + per-user lock (см. bot/state.py).
"""
from __future__ import annotations

import ssl
import sys
from pathlib import Path

from loguru import logger
from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes, TypeHandler
from telegram.request import HTTPXRequest

from bot.auth import whitelist_only
from bot.scenarios import MENU_KEYBOARD, build_conversations, build_extra_handlers
from bot.state import MAX_PER_USER_INFLIGHT, USER_QUEUE_LIMIT, get_state
from client.config import settings


# ── /start, /help ──────────────────────────────────────────────────────────
HELP_TEXT = (
    "Phygital+ bot.\n\n"
    "Кнопочное меню: /start или /menu — выбор сценария кнопками.\n\n"
    "Команды:\n"
    "• /generate — text→image. Спросит prompt → ноду → модель → ratio → resolution.\n"
    "• /img2img — image→image. 1–4 init-картинки → /done → prompt → ноду → параметры.\n"
    "• /prep_speaker — обработать фото спикера. Фото → man/woman → ratio → resolution.\n"
    "• /menu — главное меню.\n"
    "• /cancel — отменить текущий сценарий (есть и inline-кнопка «✖️ Отмена» в пикерах).\n"
    "• /help — это сообщение.\n\n"
    "Ноды: 🍌 Nano Banana (Gemini Image API) и 🤖 GPT Image 2.\n\n"
    "Ориентир по времени (откалибровано по логам, см. /menu для актуального ETA):\n"
    "• Nano Banana — ~30–60 сек на картинку\n"
    "• GPT Image 2 — ~8–9 мин на картинку\n"
    "• /prep_speaker — ~30–60 сек (ходит через Nano Banana)\n\n"
    "После каждого готового результата — три кнопки:\n"
    "• 🔄 Повторить — те же параметры, тот же промпт.\n"
    "• ✏️ Уточнить — изменить промпт (бот пришлёт исходный в код-блоке для копирования).\n"
    "• 🖼 Как img2img — использовать этот результат как init для нового image→image.\n"
    "Действия живут 24ч.\n\n"
    f"Лимиты: глобально {settings.bot_max_concurrency} задач, "
    f"на пользователя — до {MAX_PER_USER_INFLIGHT} в работе + очередь до {USER_QUEUE_LIMIT}."
)


@whitelist_only
async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Phygital+ bot. Выбери действие или жми /help.",
        reply_markup=MENU_KEYBOARD,
    )


@whitelist_only
async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


# ── error handler ──────────────────────────────────────────────────────────
async def _error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    err = ctx.error
    uid = None
    if isinstance(update, Update) and update.effective_user:
        uid = update.effective_user.id
    log = logger.bind(uid=uid or 0, where="error_handler")
    log.opt(exception=err).error(f"unhandled: {type(err).__name__}: {err}")


# ── глобальный TypeHandler: логируем каждое входящее обновление ────────────
async def _log_every_update(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    uid = u.id if u else 0
    uname = (u.username or u.full_name or "?") if u else "?"
    log = logger.bind(uid=uid, uname=uname, where="update")
    msg = update.message
    cb = update.callback_query
    if msg:
        if msg.text:
            log.info(f"msg text: {msg.text[:120]!r}")
        elif msg.photo:
            largest = msg.photo[-1]
            log.info(
                f"msg photo: {largest.width}x{largest.height} "
                f"size={largest.file_size or '?'}"
            )
        elif msg.document:
            log.info(
                f"msg document: {msg.document.mime_type} "
                f"name={msg.document.file_name!r} size={msg.document.file_size or '?'}"
            )
        else:
            log.info("msg (other)")
    elif cb:
        log.info(f"callback: {cb.data!r}")
    else:
        log.debug(f"update kind={update.update_id} (no message/cb)")


# ── HTTPX request с прокси и кастомным CA ──────────────────────────────────
def _build_httpx_kwargs() -> dict | None:
    """Готовит общие kwargs (proxy/verify) или None, если ни прокси, ни CA не заданы."""
    proxy = settings.telegram_proxy_url.strip()
    cert = settings.telegram_proxy_cert.strip()
    if not proxy and not cert:
        return None

    httpx_kwargs: dict = {}
    if cert:
        cert_path = Path(cert)
        if not cert_path.exists():
            raise SystemExit(f"TELEGRAM_PROXY_CERT не найден: {cert_path}")
        ctx = ssl.create_default_context()
        # Файл может быть PEM (текст) или DER (бинарный) — пробуем оба варианта.
        raw = cert_path.read_bytes()
        try:
            ctx.load_verify_locations(cadata=raw.decode("ascii"))
            fmt = "PEM"
        except (UnicodeDecodeError, ssl.SSLError):
            ctx.load_verify_locations(cadata=raw)
            fmt = "DER"
        httpx_kwargs["verify"] = ctx
        logger.info(f"Telegram TLS: CA из {cert_path} ({fmt})")
    else:
        try:
            import truststore  # type: ignore[import-not-found]

            httpx_kwargs["verify"] = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            logger.info("Telegram TLS: truststore (системные сертификаты)")
        except Exception:
            logger.info("Telegram TLS: дефолтный certifi")

    if proxy:
        httpx_kwargs["proxy"] = proxy  # переносим в httpx-уровень в _make_request
        logger.info(f"Telegram прокси: {proxy}")

    return httpx_kwargs


def _make_request(pool_size: int, kwargs_template: dict | None) -> HTTPXRequest | None:
    """Создаёт HTTPXRequest с нужным размером пула. None — если без прокси/CA нет смысла."""
    if kwargs_template is None:
        # без прокси/CA, но всё равно увеличим пул
        return HTTPXRequest(connection_pool_size=pool_size)
    kw = dict(kwargs_template)
    proxy = kw.pop("proxy", None)
    req_kwargs: dict = {"httpx_kwargs": kw, "connection_pool_size": pool_size}
    if proxy:
        req_kwargs["proxy"] = proxy
    return HTTPXRequest(**req_kwargs)


PREFLIGHT_REFRESH_THRESHOLD_SEC = 15 * 60  # access JWT TTL ниже которого пробуем refresh


# ── post-init: сетим меню команд и прогреваем state ────────────────────────
async def _post_init(app: Application) -> None:
    # Прогреваем state (fail-fast, если сессия Phygital не загружается).
    state = get_state()
    await _preflight_session(state)
    await app.bot.set_my_commands(
        [
            BotCommand("menu", "Главное меню (кнопки)"),
            BotCommand("generate", "Сгенерировать по тексту"),
            BotCommand("img2img", "Image→image: картинки + промпт"),
            BotCommand("prep_speaker", "Обработать фото спикера"),
            BotCommand("cancel", "Отменить текущий сценарий"),
            BotCommand("help", "Справка"),
        ]
    )
    logger.info("Bot commands registered")


async def _preflight_session(state) -> None:
    """Проверяет, что сессия живая. Если access-JWT почти истёк — делает refresh
    (с авто-fallback на свежайший recon-дамп). Если ничего не помогло —
    останавливаем процесс с понятной инструкцией."""
    sess = state.session
    ttl = sess.jwt_ttl_seconds()
    if ttl is None:
        logger.warning("Pre-flight: access-token JWT не парсится, пробую refresh")
    elif ttl >= PREFLIGHT_REFRESH_THRESHOLD_SEC:
        logger.info(f"Pre-flight: access JWT TTL={ttl}s ({ttl // 60}m) — refresh не нужен")
        return
    else:
        logger.info(f"Pre-flight: access JWT TTL={ttl}s — делаю refresh")

    try:
        await state.session_manager.refresh(sess)
        new_ttl = sess.jwt_ttl_seconds()
        logger.info(f"Pre-flight: refresh ок, новый TTL={new_ttl}s ({(new_ttl or 0) // 60}m)")
    except Exception as e:
        logger.opt(exception=e).error(f"Pre-flight: refresh не удался: {e}")
        raise SystemExit(
            f"Сессия Phygital мертва: {e}\n"
            "Сделай свежий recon: .venv/bin/python -m recon.capture\n"
            "После этого бот сам подхватит storage-*.json при старте."
        )


def build_app() -> Application:
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан в .env")
    if not settings.allowed_user_ids:
        logger.warning("TELEGRAM_ALLOWED_USER_IDS пуст — бот будет открыт ВСЕМ!")

    builder = Application.builder().token(settings.telegram_bot_token)
    tmpl = _build_httpx_kwargs()
    # Разделяем пулы: long-polling (getUpdates) держит коннект бесконечно,
    # action-запросы (sendMessage / getFile / download) идут через отдельный пул.
    action_req = _make_request(pool_size=settings.bot_max_concurrency * 4, kwargs_template=tmpl)
    poll_req = _make_request(pool_size=1, kwargs_template=tmpl)
    if action_req is not None:
        builder = builder.request(action_req)
    if poll_req is not None:
        builder = builder.get_updates_request(poll_req)
    builder = builder.post_init(_post_init)
    app = builder.build()

    # Group=-1: лог всех апдейтов до того, как их разберут conversations.
    app.add_handler(TypeHandler(Update, _log_every_update), group=-1)
    # /start, /help — регистрируем ДО conversation, чтобы они всегда срабатывали.
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    # Conversations
    for conv in build_conversations():
        app.add_handler(conv)
    # Стандалон-хендлеры пост-задачных действий и меню.
    # Каждая пара (bucket, handler): "group0" = тот же group что у conversations,
    # "group1" = ниже приоритетом (чтобы активные ConversationHandler-MessageHandler'ы
    # забирали текст пользователя первыми).
    for bucket, handler in build_extra_handlers():
        group = 0 if bucket == "group0" else 1
        app.add_handler(handler, group=group)
    app.add_error_handler(_error_handler)
    return app


LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def _setup_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | "
               "<cyan>{extra[uid]}</cyan> | <level>{message}</level>",
        filter=lambda r: r["extra"].setdefault("uid", "-") or True,
    )
    LOG_DIR.mkdir(exist_ok=True)
    logger.add(
        LOG_DIR / "bot.log",
        level="DEBUG",
        rotation="20 MB",
        retention=5,
        enqueue=True,
        backtrace=True,
        diagnose=False,  # не печатать значения локальных переменных (могут быть токены)
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | uid={extra[uid]} | "
               "{name}:{function}:{line} | {message}",
        filter=lambda r: r["extra"].setdefault("uid", "-") or True,
    )


def main() -> None:
    _setup_logging()
    app = build_app()
    logger.info(
        f"Bot started (whitelist={len(settings.allowed_user_ids)} uids, "
        f"max_concurrency={settings.bot_max_concurrency})"
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
