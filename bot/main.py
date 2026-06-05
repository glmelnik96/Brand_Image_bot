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

import asyncio
import hashlib
import os
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
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)
from telegram.request import HTTPXRequest

from bot.auth import whitelist_only
from bot.scenarios import _menu_root_kb_for, build_conversations, build_extra_handlers
from bot.state import MAX_PER_USER_INFLIGHT, USER_QUEUE_LIMIT, get_state
from client.config import ROOT, settings


# ── /start, /help ──────────────────────────────────────────────────────────
HELP_TEXT = (
    "Brand Image Bot — генерация и редактирование картинок.\n\n"
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
    "Бот обновился и снова в строю — работает до конца рабочего дня!\n\n"
    "Что нового:\n"
    "— Под Render и 2d Isometry появилась кнопка «Удалить фон» — PNG с прозрачностью.\n"
    "— Под портретом спикера тоже есть «Удалить фон».\n"
    "— Статус генерации теперь показывает реальный процент прогресса.\n"
    "— Если Gemini Pro упал (ошибка / 504) — автоматически пробую Flash, "
    "чтобы сценарий доехал до результата.\n"
    "— Сценарий «Обычное изображение» временно отключён — пока чиним. "
    "Все бренд-сценарии (Photo / Render / 2d Isometry) и редактирование работают как раньше.\n\n"
    "ВАЖНО! Пройдите, пожалуйста, короткий опрос — это реально важно!!!\n"
    "Всего 12 вопросов, вопросы можно пропускать, займёт пару минут.\n"
    "Без вашего фидбэка бот развивается вслепую — нужно понимать, что докрутить дальше.\n"
    "Открыть опрос: /menu → «Обратная связь» → «Пройти опрос».\n\n"
    "И самое главное — пользуйтесь ботом для СВОИХ РАБОЧИХ задач!!! "
    "Генерьте картинки под свои задачи, презентации, посты, лендинги — всё, что нужно. "
    "Чем больше реальных кейсов прогоните — тем быстрее бот станет полезнее именно вам."
)


@whitelist_only
async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else None
    await update.message.reply_text(
        "Привет! Это Brand Image Bot — генерация и редактирование картинок.\n"
        "Выбери сценарий кнопкой ниже или открой /help, если нужны подробности.",
        reply_markup=_menu_root_kb_for(uid),
    )


@whitelist_only
async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


# ── /admin_stat — приватная админская сводка по пользователям ─────────────
# Доступна только UID 438074662 (Глеб). Парсит logs/bot.log* через
# tools.digest.collect_user_stats и шлёт Markdown-like HTML-вывод в чат.
# Поддерживает аргумент: /admin_stat 24h | 7d | YYYY-MM-DD (дефолт: 7d).
ADMIN_STAT_UID = 438074662


async def cmd_admin_stat(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u or u.id != ADMIN_STAT_UID:
        # Тихо игнорируем — команда не должна светиться остальным.
        return
    # late import: tools/digest не нужен в импортном графе старта бота.
    from tools.digest import collect_user_stats, format_user_stats, parse_since

    arg = " ".join(ctx.args).strip() if ctx.args else "7d"
    try:
        since = parse_since(arg)
    except SystemExit as e:
        await update.message.reply_text(f"Ошибка аргумента: {e}")
        return
    try:
        stats = collect_user_stats(since=since)
        text = format_user_stats(stats, since=since)
    except Exception as e:
        logger.opt(exception=e).warning(f"admin_stat: collect failed: {e!r}")
        await update.message.reply_text(f"Ошибка: {type(e).__name__}: {e}")
        return
    # Telegram-лимит 4096 символов на сообщение — режем при необходимости.
    chunks: list[str] = []
    cur = ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > 3800:
            chunks.append(cur)
            cur = line
        else:
            cur += line
    if cur:
        chunks.append(cur)
    for ch in chunks:
        await update.message.reply_text(ch, parse_mode="HTML")


# ── /admin_surveys — полный дамп ответов опросов (только UID ADMIN_STAT_UID) ──
# Поддерживает arg: /admin_surveys [N] — сколько последних опросов показать (по
# умолчанию 20). Чанкуется на 3800 символов.
async def cmd_admin_surveys(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u or u.id != ADMIN_STAT_UID:
        return
    limit = 20
    if ctx.args:
        try:
            limit = max(1, min(200, int(ctx.args[0])))
        except ValueError:
            await update.message.reply_text("Аргумент должен быть числом (1..200).")
            return
    from bot.feedback import get_feedback_store
    try:
        text = get_feedback_store().format_surveys_dump(limit=limit)
    except Exception as e:
        logger.opt(exception=e).warning(f"admin_surveys: dump failed: {e!r}")
        await update.message.reply_text(f"Ошибка: {type(e).__name__}: {e}")
        return
    chunks: list[str] = []
    cur = ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > 3800:
            chunks.append(cur)
            cur = line
        else:
            cur += line
    if cur:
        chunks.append(cur)
    for ch in chunks:
        await update.message.reply_text(ch, parse_mode="HTML")


# ── error handler ──────────────────────────────────────────────────────────
async def _error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    err = ctx.error
    uid = None
    if isinstance(update, Update) and update.effective_user:
        uid = update.effective_user.id
    log = logger.bind(uid=uid or 0, where="error_handler")
    log.opt(exception=err).error(f"unhandled: {type(err).__name__}: {err}")
    # Если упало по причине протухшей Phygital-сессии — пингуем админа.
    if err is not None:
        name = type(err).__name__
        if name in ("PhygitalAuthError", "RefreshError"):
            try:
                await _notify_admin_auth_required(ctx.application, f"{name}: {err}")
            except Exception as e:
                logger.opt(exception=e).warning(f"admin notify from error_handler: {e!r}")


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

# ── админ-нотификации про мёртвую сессию ──────────────────────────────────
# Когда фоновый рефрешер или таск ловят PhygitalAuthError / 418, мы пингуем
# ADMIN_STAT_UID в TG. Throttle, чтобы не заспамить.
import subprocess as _subprocess
import time as _time

AUTH_NOTIFY_THROTTLE_SEC = 10 * 60  # одно сообщение раз в 10 мин
_last_auth_notify_ts: float = 0.0
# Хэндл живого процесса refresh_capture --show (Popen), чтобы не запускать второй параллельно.
_active_auth_proc: _subprocess.Popen | None = None


def _spawn_visible_recon() -> tuple[bool, str]:
    """Запускает recon/refresh_capture в видимом режиме (-показывает Chromium).

    Не блокирует — возвращает сразу. Если процесс уже жив, ничего не делает.
    Возвращает (started, message).
    """
    global _active_auth_proc
    if _active_auth_proc is not None and _active_auth_proc.poll() is None:
        return False, f"окно логина уже открыто (pid={_active_auth_proc.pid})"
    try:
        # CREATE_NEW_CONSOLE на Windows, чтобы Chromium вылез отдельным окном
        # и не блокировал stdout бота.
        creationflags = 0
        if sys.platform == "win32":
            creationflags = _subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
        _active_auth_proc = _subprocess.Popen(
            [sys.executable, "-m", "recon.refresh_capture", "--show"],
            cwd=str(ROOT),
            creationflags=creationflags,
        )
        return True, f"процесс запущен (pid={_active_auth_proc.pid})"
    except Exception as e:
        return False, f"не удалось запустить: {type(e).__name__}: {e}"


async def _notify_admin_auth_required(
    app: Application,
    reason: str,
    *,
    force: bool = False,
) -> None:
    """Шлёт админу TG-сообщение про мёртвую сессию + inline-кнопку «Открыть окно логина».

    Throttle AUTH_NOTIFY_THROTTLE_SEC секунд между сообщениями (можно перебить force=True).
    """
    global _last_auth_notify_ts
    now = _time.time()
    if not force and (now - _last_auth_notify_ts) < AUTH_NOTIFY_THROTTLE_SEC:
        return
    _last_auth_notify_ts = now
    try:
        state = get_state()
        ttl = state.session.jwt_ttl_seconds()
        ttl_str = f"{ttl}s" if ttl is not None else "?"
        captured = getattr(state.session, "captured_at", None)
        captured_str = captured.isoformat(timespec="seconds") if captured else "?"
    except Exception:
        ttl_str, captured_str = "?", "?"
    text = (
        "⚠️ <b>Phygital-сессия отвалилась</b>\n\n"
        f"Причина: <code>{reason[:300]}</code>\n"
        f"Текущий access TTL: <code>{ttl_str}</code>\n"
        f"captured_at: <code>{captured_str}</code>\n\n"
        "Жми кнопку — открою окно логина на этой машине.\n"
        "После логина (или если профиль ещё жив) окно закроется само, "
        "бот подхватит свежие токены."
    )
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Открыть окно логина", callback_data="admin:auth_open")]]
    )
    try:
        await app.bot.send_message(
            ADMIN_STAT_UID, text, parse_mode="HTML", reply_markup=kb,
        )
        logger.info(f"admin auth notify: отправлено uid={ADMIN_STAT_UID} reason={reason!r}")
    except Exception as e:
        logger.opt(exception=e).warning(f"admin auth notify: не доставлено: {e!r}")


# Команда /admin_auth — открыть окно логина по запросу админа (без ожидания падения).
async def cmd_admin_auth(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u or u.id != ADMIN_STAT_UID:
        return
    started, msg = _spawn_visible_recon()
    head = "Открываю окно логина…" if started else "Окно НЕ открыто:"
    await update.message.reply_text(
        f"{head}\n<code>{msg}</code>\n\n"
        "После успешного логина (или если профиль ещё жив) свежий дамп "
        "подхватится автоматически — либо ближайшим тиком фонового рефрешера, "
        "либо при следующей попытке refresh.",
        parse_mode="HTML",
    )


# Callback под inline-кнопкой из админ-нотификации.
async def cb_admin_auth_open(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    u = update.effective_user
    if not u or u.id != ADMIN_STAT_UID:
        await q.answer("Только для админа.", show_alert=False)
        return
    started, msg = _spawn_visible_recon()
    await q.answer("Запускаю…" if started else "Не запустилось", show_alert=False)
    try:
        await q.edit_message_text(
            ("✅ <b>Окно логина запущено</b>\n" if started
             else "⚠️ <b>Не удалось запустить</b>\n")
            + f"<code>{msg}</code>\n\n"
            "Бот подхватит свежий дамп автоматически.",
            parse_mode="HTML",
        )
    except Exception:
        pass  # сообщение могло быть слишком старым — не критично


# Фоновый рефрешер сессии: дёргает recon/refresh_capture (headless persistent profile),
# чтобы Phygital не успел протухнуть refresh-токен между задачами. По умолчанию каждые 30 мин.
# Отключается через BOT_AUTO_REFRESH_INTERVAL_MIN=0.
_DEFAULT_AUTO_REFRESH_MIN = 30
try:
    AUTO_REFRESH_INTERVAL_MIN = int(os.environ.get("BOT_AUTO_REFRESH_INTERVAL_MIN", _DEFAULT_AUTO_REFRESH_MIN))
except ValueError:
    AUTO_REFRESH_INTERVAL_MIN = _DEFAULT_AUTO_REFRESH_MIN


async def _auto_refresh_session_loop(app: Application, state) -> None:
    """Фоном держит сессию Phygital живой.

    Каждые AUTO_REFRESH_INTERVAL_MIN минут:
      1) гоняет recon.refresh_capture.main(headless=True) — снимает свежий
         storage-*.json через персистентный профиль user_data/;
      2) подсовывает этот дамп в текущую in-memory сессию через
         SessionManager._find_fresher_recon_dump + _swap_session_inplace;
      3) персистит обновлённую сессию на диск.

    Если rc==2 (профиль user_data разлогинен) — шлём админу TG-сообщение
    с кнопкой «Открыть окно логина». Throttle внутри _notify_admin_auth_required.

    Любые исключения внутри итерации логируются и проглатываются — луп никогда
    не падает. CancelledError пробрасывается чисто.
    """
    interval_sec = max(60, AUTO_REFRESH_INTERVAL_MIN * 60)
    logger.info(
        f"session auto-refresh: интервал {AUTO_REFRESH_INTERVAL_MIN} мин "
        f"(headless recon-capture)"
    )
    # Первая итерация — спим, чтобы не конкурировать со стартовым _preflight_session.
    while True:
        try:
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            logger.info("session auto-refresh: cancelled")
            raise
        try:
            from recon import refresh_capture  # late import: playwright тянется лениво
            rc = await refresh_capture.main(headless=True)
            if rc == 0:
                fresh = state.session_manager._find_fresher_recon_dump(state.session)
                if fresh is not None:
                    state.session_manager._swap_session_inplace(state.session, fresh)
                    state.session_manager.save(state.session)
                    new_ttl = state.session.jwt_ttl_seconds()
                    logger.info(
                        f"session auto-refresh: подхватил {fresh.name}, "
                        f"новый TTL={new_ttl}s ({(new_ttl or 0) // 60}m)"
                    )
                else:
                    logger.warning("session auto-refresh: дамп сохранён, но fresher не найден (mtime / TTL фильтры)")
            elif rc == 2:
                logger.warning("session auto-refresh: профиль user_data разлогинен — нужен ручной recon.capture")
                await _notify_admin_auth_required(
                    app,
                    "headless recon: профиль user_data разлогинен (rc=2)",
                )
            else:
                logger.warning(f"session auto-refresh: recon вернул rc={rc}")
                await _notify_admin_auth_required(app, f"headless recon вернул rc={rc}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.opt(exception=e).warning(f"session auto-refresh tick crashed: {e!r}")
            await _notify_admin_auth_required(app, f"refresh tick crashed: {type(e).__name__}: {e}")


# ── post-init: сетим меню команд и прогреваем state ────────────────────────
async def _post_init(app: Application) -> None:
    # Прогреваем state (fail-fast, если сессия Phygital не загружается).
    state = get_state()
    try:
        await _preflight_session(state)
    except SystemExit as e:
        # Перед тем как умереть — успеваем пингануть админа с кнопкой.
        try:
            await _notify_admin_auth_required(app, f"preflight failed: {e}", force=True)
        except Exception as ee:
            logger.opt(exception=ee).warning(f"admin notify on preflight fail: {ee!r}")
        raise
    public_cmds = [
        BotCommand("menu", "Главное меню"),
        BotCommand("start", "Главное меню"),
        BotCommand("cancel", "Отменить текущий сценарий"),
        BotCommand("help", "Справка"),
    ]
    await app.bot.set_my_commands(public_cmds)
    # Для админа — расширенный список с админ-командами поверх публичных.
    # scope=BotCommandScopeChat(chat_id=ADMIN_STAT_UID) пишет персональный набор
    # команд только в его меню /-кнопок (для остальных юзеров не виден).
    admin_cmds = public_cmds + [
        BotCommand("admin_stat", "Админ: статистика по пользователям (7d / 24h / N)"),
        BotCommand("admin_surveys", "Админ: дамп ответов опросов"),
        BotCommand("admin_auth", "Админ: открыть окно логина Phygital"),
    ]
    try:
        await app.bot.set_my_commands(
            admin_cmds,
            scope=BotCommandScopeChat(chat_id=ADMIN_STAT_UID),
        )
        logger.info(f"Admin commands set for uid={ADMIN_STAT_UID}")
    except Exception as e:
        logger.opt(exception=e).warning(f"set admin commands failed: {e!r}")
    logger.info("Bot commands registered")
    # Фоновый рефрешер Phygital-сессии. Не валит запуск, если плеяwright нет.
    if AUTO_REFRESH_INTERVAL_MIN > 0:
        try:
            app.bot_data["_session_refresher_task"] = asyncio.create_task(
                _auto_refresh_session_loop(app, state),
                name="session-refresher",
            )
        except Exception as e:
            logger.opt(exception=e).warning(f"session auto-refresh: не удалось запустить: {e!r}")
    else:
        logger.info("session auto-refresh: отключён (BOT_AUTO_REFRESH_INTERVAL_MIN=0)")
    # Стартовый broadcast (один раз на версию текста). Не валим запуск при ошибках.
    try:
        await _startup_broadcast(app)
    except Exception as e:
        logger.opt(exception=e).warning(f"startup broadcast crashed: {e!r}")


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
    app = builder.build()

    # Group=-1: лог всех апдейтов до того, как их разберут conversations.
    app.add_handler(TypeHandler(Update, _log_every_update), group=-1)
    # /start, /help — регистрируем ДО conversation, чтобы они всегда срабатывали.
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    # /admin_stat — приватный, доступен только UID ADMIN_STAT_UID. Внутри cmd_admin_stat
    # проверка по uid; для остальных команда «не существует» (silent return).
    app.add_handler(CommandHandler("admin_stat", cmd_admin_stat))
    app.add_handler(CommandHandler("admin_surveys", cmd_admin_surveys))
    app.add_handler(CommandHandler("admin_auth", cmd_admin_auth))
    app.add_handler(CallbackQueryHandler(cb_admin_auth_open, pattern=r"^admin:auth_open$"))
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
