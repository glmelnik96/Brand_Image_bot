"""Entry point Telegram-бота.

Поддерживает:
- HTTP-прокси для api.telegram.org (Cloud.ru kwts MITM) + кастомный CA pem.
- Whitelist по user_id (`TELEGRAM_ALLOWED_USER_IDS`).
- Единое меню (/menu или /start): «Создай / Изменить / Спикер / Помощь»
  с подменю Photo/Render/2d Isometry для brand text→image.
- Глобальный лимит одновременных задач + per-user lock (см. bot/state.py).
- Startup-broadcast с маркером версии: при изменении STARTUP_BROADCAST_TEXT
  бот один раз шлёт уведомление всем allowed_user_ids, помечает файл в storage/.
"""
from __future__ import annotations

import hashlib
import ssl
import sys
from pathlib import Path

# Под Windows stdout/stderr по умолчанию cp1251 — loguru пишет в stderr Unicode-сообщения
# (эмодзи, стрелки в логах), которые валят процесс UnicodeEncodeError. Переключаем явно.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass

from loguru import logger
from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes, TypeHandler
from telegram.request import HTTPXRequest

from bot.auth import whitelist_only
from bot.scenarios import MENU_KEYBOARD, build_conversations, build_extra_handlers
from bot.state import MAX_PER_USER_INFLIGHT, USER_QUEUE_LIMIT, get_state
from client.config import ROOT, settings


# ── /start, /help ──────────────────────────────────────────────────────────
HELP_TEXT = (
    "Phygital+ bot — генерация и редактирование картинок.\n\n"
    "Управление — только меню (/menu или /start). Слэш-команды-ярлыки сняты,\n"
    "чтобы один и тот же путь шёл через кнопки.\n\n"
    "Структура меню:\n"
    "• Создай изображение\n"
    "    └ Бренд изображения\n"
    "         ├ Photo       — фотореалистичный брендовый стиль Cloud.ru\n"
    "         ├ Render      — 3D-объекты и продуктовые рендеры\n"
    "         └ 2d Isometry — 2D-изометрические сцены и иллюстрации\n"
    "    └ Обычное изображение — text→image напрямую в Nano Banana\n"
    "• Изменить изображение\n"
    "    ├ Изменить изображение — img2img: исходники + новый текст\n"
    "    └ Добавить Brand patterns — брендовая обработка картинок (Gemini сам опишет)\n"
    "• Фотография спикера — портрет спикера по референсу\n"
    "• Помощь — это сообщение\n\n"
    "Бренд-сценарии = Gemini Text → Nano Banana (+~30 сек). У каждого Photo/Render/\n"
    "Isometry свой system-prompt в docs/SYSTEM_PROMPT_Gemini3Pro_CloudRu_*.md.\n\n"
    "Команды:\n"
    "• /menu, /start — главное меню.\n"
    "• /cancel — выйти из текущего сценария (или нажать «Отмена» в пикере).\n"
    "• /help — это сообщение.\n\n"
    "Модель: Nano Banana (Gemini Image API), версия 3.1 Pro.\n"
    "Время на одну картинку — ориентировочно 30–60 секунд "
    "(брендовые — 60–120 сек, два task'а подряд).\n\n"
    "Кнопки под результатом (актуальны 24 часа):\n"
    "• Повторить — есть в каждом сценарии.\n"
    "• Изменить текст — только под результатами «Обычное изображение»\n"
    "  (единственный сценарий с пользовательским текстом).\n"
    "• Изменить изображение — взять результат как исходник для img2img.\n"
    "• Добавить Brand patterns — только под результатами «Обычное изображение»;\n"
    "  отправляет результат в brand-img2img.\n\n"
    f"Лимиты: всего {settings.bot_max_concurrency} задач одновременно, "
    f"на пользователя — до {MAX_PER_USER_INFLIGHT} в работе плюс очередь до {USER_QUEUE_LIMIT}."
)


# Стартовое объявление. Текст хэшируется sha256 → файл-маркер в storage/.
# Меняешь текст → меняется хэш → бот при следующем запуске разошлёт повторно
# и положит новый marker-файл. Без смены текста — повторных рассылок нет.
STARTUP_BROADCAST_TEXT = (
    "Бот обновлён и снова на связи.\n\n"
    "Главное меню перестроено — открывается одной командой /menu или /start.\n"
    "Бренд-генерация теперь делится на три варианта: Photo, Render и 2d Isometry.\n"
    "Слэш-команды сценариев убраны: всё доступно через кнопки.\n\n"
    "Если что-то ведёт себя странно — пиши, разберёмся."
)


# Краткая сводка последних фиксов — попадает в DAILY_STARTUP_NOTICE_TEXT.
# Обновляй здесь при каждом значимом релизе: 1–2 предложения.
LAST_FIXES_SUMMARY = (
    "Переписаны промпты «Фотографии спикера» — портрет точнее держит лицо и "
    "не рисует красную метку из референса. Под готовым портретом появились "
    "кнопки смены фона на брендовые цвета (зелёный, лайм, фиолетовый, "
    "голубой, чёрный)."
)


# Ежедневный startup-notice. Шлётся ВСЕГДА при запуске (без sha256-маркера) —
# в отличие от STARTUP_BROADCAST_TEXT, который рассылается один раз на версию текста.
DAILY_STARTUP_NOTICE_TEXT = (
    "Бот вышел на смену и работает до конца рабочего дня.\n"
    "Если что-то идёт не так — пиши Глебу.\n\n"
    f"Последние фиксы: {LAST_FIXES_SUMMARY}"
)


# Сообщение при остановке. Шлётся ВСЕГДА (через post_stop-хук PTB).
SHUTDOWN_NOTICE_TEXT = "Бот ушёл на перерыв. До завтра."


@whitelist_only
async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Это Phygital+ bot — генерация и редактирование картинок.\n"
        "Выбери сценарий кнопкой ниже или открой /help, если нужны подробности.",
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
            BotCommand("menu", "Главное меню"),
            BotCommand("start", "Главное меню"),
            BotCommand("cancel", "Отменить текущий сценарий"),
            BotCommand("help", "Справка"),
        ]
    )
    logger.info("Bot commands registered")
    # Стартовый broadcast (один раз на версию текста). Не валим запуск при ошибках.
    try:
        await _startup_broadcast(app)
    except Exception as e:
        logger.opt(exception=e).warning(f"startup broadcast crashed: {e!r}")
    # Ежедневное «бот на смене» — шлётся при каждом запуске, без sha-маркера.
    try:
        await _broadcast_to_allowed(app, DAILY_STARTUP_NOTICE_TEXT, tag="daily startup notice")
    except Exception as e:
        logger.opt(exception=e).warning(f"daily startup notice crashed: {e!r}")


async def _post_stop(app: Application) -> None:
    """Хук вызывается между Updater.stop() и Application.shutdown() —
    bot ещё может отправлять сообщения. Шлём «ушёл на перерыв» и не валим
    остановку при ошибках сети."""
    try:
        await _broadcast_to_allowed(app, SHUTDOWN_NOTICE_TEXT, tag="shutdown notice")
    except Exception as e:
        logger.opt(exception=e).warning(f"shutdown notice crashed: {e!r}")


async def _broadcast_to_allowed(app: Application, text: str, *, tag: str) -> None:
    """Разослать сообщение всем allowed_user_ids. В отличие от _startup_broadcast
    не использует sha256-маркер — шлёт всегда. Ошибки по конкретным uid не валят
    рассылку остальным."""
    text = text.strip()
    if not text:
        return
    uids = list(settings.allowed_user_ids)
    if not uids:
        logger.info(f"{tag}: allowed_user_ids пуст — нечего рассылать")
        return
    logger.info(f"{tag}: рассылаю на {len(uids)} uid(ов)")
    sent = 0
    for uid in uids:
        try:
            await app.bot.send_message(uid, text)
            sent += 1
        except Exception as e:
            logger.warning(f"{tag} → {uid} failed: {type(e).__name__}: {e}")
    logger.info(f"{tag} done: sent={sent}/{len(uids)}")


# storage/startup_broadcast.<digest>.flag — маркер «эту версию текста мы уже разослали».
_BROADCAST_MARKER_DIR = ROOT / "storage"


async def _startup_broadcast(app: Application) -> None:
    """Разослать STARTUP_BROADCAST_TEXT всем allowed_user_ids один раз на версию текста.
    Версия = sha256(текст)[:12]. Если файл-маркер с этой версией уже есть — пропускаем."""
    text = STARTUP_BROADCAST_TEXT.strip()
    if not text:
        return
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    marker = _BROADCAST_MARKER_DIR / f"startup_broadcast.{digest}.flag"
    if marker.exists():
        logger.info(f"startup broadcast: marker {marker.name} exists — skip")
        return
    uids = list(settings.allowed_user_ids)
    if not uids:
        logger.info("startup broadcast: allowed_user_ids пуст — нечего рассылать")
        return
    logger.info(f"startup broadcast: рассылаю версию {digest} на {len(uids)} uid(ов)")
    sent: list[int] = []
    failed: list[tuple[int, str]] = []
    for uid in uids:
        try:
            await app.bot.send_message(uid, text)
            sent.append(uid)
        except Exception as e:
            failed.append((uid, f"{type(e).__name__}: {e}"))
            logger.warning(f"startup broadcast → {uid} failed: {type(e).__name__}: {e}")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        f"version={digest}\nsent={len(sent)}/{len(uids)}\n"
        f"sent_uids={sent}\nfailed={failed}\n",
        encoding="utf-8",
    )
    logger.info(
        f"startup broadcast done: sent={len(sent)}/{len(uids)}, "
        f"marker={marker.name}"
    )


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
            "Сделай свежий recon:\n"
            "  Windows: .venv\\Scripts\\python -m recon.capture\n"
            "  macOS/Linux: .venv/bin/python -m recon.capture\n"
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
    builder = builder.post_stop(_post_stop)
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
