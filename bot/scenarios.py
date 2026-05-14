"""Сценарии бота и пост-задачные действия.

Модуль делится на пять смысловых блоков:

  1. Константы (state IDs, ETA, GEMINI_*, GPT_*, NODES).
  2. Вспомогательные функции (`_kb`, `_action_keyboard`, `_check_user_capacity`,
     `_send_result_image`, `_save_recipe_for_task`, `_rerun_from_recipe`).
  3. Очередь и выполнение задач (`_enqueue_task`, `_execute_task`).
     В success-ветке `_execute_task`:
       - скачивает первую result-картинку в `regen_cache/<uid>/<task_uid>/`;
       - копирует init-файлы туда же;
       - создаёт TaskRecipe и кладёт в `state.recipes`;
       - отправляет результат вместе с inline-клавиатурой действий
         (🔄 Повторить / ✏️ Уточнить / 🖼 Как img2img).
  4. ConversationHandler-сценарии: /generate, /img2img, /prep_speaker.
     Каждый entry-point поддерживает оба способа вызова — командой и через menu-callback
     (см. `_first_message`).
  5. Пост-задачные callback-хендлеры и стандалон-хендлеры
     (`regen_cb`, `edit_prompt_cb`, `as_i2i_cb`, `pending_edit_listener`, `menu_router`).
     Регистрируются отдельно от ConversationHandler через `build_extra_handlers`.

Внешние точки:
  - `build_conversations()` — список ConversationHandler для регистрации в Application.
  - `build_extra_handlers()` — список стандалон-хендлеров (cmd_menu, menu_router,
    regen_cb, edit_prompt_cb, pending_edit_listener).

Логирование: на каждом шаге FSM `_ulog(update, action)` пишет uid + username + сценарий;
`_execute_task` пишет цикл queued → started → finished с таймингами и job_id.

Конкурентность: см. `bot/state.py` — глобальный семафор + per-user FIFO + per-user Semaphore.
"""
from __future__ import annotations

import dataclasses
import html
import shutil
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable

import httpx
from loguru import logger
from telegram import (
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Message,
    Update,
)
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.auth import whitelist_only
from bot.state import USER_QUEUE_LIMIT, TaskRecipe, get_state
from client.api import _SSL_CTX, PhygitalClient
from client.models import GenerationJob
from workflows.gpt_image import GPTImageWorkflow
from workflows.image_gen import ImageGenWorkflow
from workflows.image_to_image import ImageToImageWorkflow
from workflows.speaker_prep import (
    DEFAULT_REFERENCE,
    build_speaker_prep_workflow,
    speaker_prompt,
)

# ── state IDs ──────────────────────────────────────────────────────────────
# /generate
GEN_PROMPT, GEN_NODE, GEN_MODEL, GEN_RATIO, GEN_RES = range(1, 6)
# /generate (GPT Image branch)
GEN_GPT_QUALITY, GEN_GPT_ASPECT, GEN_GPT_BG = range(6, 9)
# /img2img
I2I_COLLECT, I2I_PROMPT, I2I_NODE, I2I_MODEL, I2I_RATIO, I2I_RES = range(10, 16)
# /img2img (GPT Image branch)
I2I_GPT_QUALITY, I2I_GPT_ASPECT, I2I_GPT_BG = range(16, 19)
# /prep_speaker
SP_PHOTO, SP_GENDER, SP_RATIO, SP_RES = range(20, 24)

TG_PHOTO_LIMIT_BYTES = 10 * 1024 * 1024

# Ориентиры по времени генерации.
# Nano Banana — медиана ~37s из реальных логов (6 задач 13 мая: 27–87s).
# GPT Image — пока берём от Phygital `averageTimeInSeconds=500`, перекалибруем
# когда накопим тайминги.
ETA_NANO_BANANA_SEC = 45
ETA_GPT_IMAGE_SEC = 500


def _fmt_eta(seconds: int) -> str:
    """'~3.5 мин' / '~8 мин'."""
    m = seconds / 60.0
    if m < 1:
        return f"~{int(seconds)} сек"
    if m < 10:
        return f"~{m:.1f} мин"
    return f"~{int(m)} мин"

# ── параметры нод (взяты из GET /api/v2/nodes/ recon) ──────────────────────
# Gemini Image API (Nano Banana), workflow id=94
GEMINI_MODELS: list[tuple[str, str]] = [
    ("v3", "v3"),
    ("v3_1", "v3.1 (Pro)"),
]
GEMINI_RATIOS: list[tuple[str, str]] = [
    ("default", "auto"),
    ("r_1_1", "1:1"),
    ("r_3_4", "3:4"),
    ("r_4_3", "4:3"),
    ("r_9_16", "9:16"),
    ("r_16_9", "16:9"),
    ("r_2_3", "2:3"),
    ("r_3_2", "3:2"),
]
GEMINI_RESOLUTIONS: list[tuple[str, str]] = [
    ("default", "auto"),
    ("k1", "1K"),
    ("k2", "2K"),
    ("k4", "4K"),
]

NODES: list[tuple[str, str]] = [
    ("nb", "🍌 Nano Banana"),
    ("gpt", "🤖 GPT Image 2"),
]

# GPT Image API, workflow id=98 (значения enum из /api/v2/nodes/ recon)
GPT_QUALITIES: list[tuple[str, str]] = [
    ("High", "High"),
    ("Medium", "Medium"),
    ("Low", "Low"),
]
GPT_ASPECTS: list[tuple[str, str]] = [
    ("auto", "auto"),
    ("r_1024_1024", "1024²"),
    ("r_1536_1024", "1536×1024"),
    ("r_1024_1536", "1024×1536"),
    ("r_2048_2048", "2048²"),
    ("r_2048_1152", "2048×1152"),
    ("r_1152_2048", "1152×2048"),
    ("r_3840_2160", "3840×2160"),
    ("r_2160_3840", "2160×3840"),
]
GPT_BACKGROUNDS: list[tuple[str, str]] = [
    ("auto", "auto"),
    ("transparent", "transparent"),
    ("opaque", "opaque"),
]


# ── inline-keyboard helpers ────────────────────────────────────────────────
def _kb(
    prefix: str,
    items: list[tuple[str, str]],
    cols: int = 3,
    with_cancel: bool = True,
) -> InlineKeyboardMarkup:
    """Клавиатура пикера. По умолчанию внизу строка [✖️ Отмена]."""
    rows: list[list[InlineKeyboardButton]] = []
    cur: list[InlineKeyboardButton] = []
    for value, label in items:
        cur.append(InlineKeyboardButton(label, callback_data=f"{prefix}:{value}"))
        if len(cur) == cols:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    if with_cancel:
        rows.append([InlineKeyboardButton("✖️ Отмена", callback_data="cancel:picker")])
    return InlineKeyboardMarkup(rows)


def _action_keyboard(task_uid: str) -> InlineKeyboardMarkup:
    """Клавиатура под готовый результат — три кнопки повторного использования."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Повторить", callback_data=f"regen:{task_uid}"),
        InlineKeyboardButton("✏️ Уточнить", callback_data=f"editp:{task_uid}"),
        InlineKeyboardButton("🖼 Как img2img", callback_data=f"asi2i:{task_uid}"),
    ]])


MENU_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("🎨 Сгенерировать (text→image)", callback_data="menu:generate")],
    [InlineKeyboardButton("🖼 Image→Image", callback_data="menu:img2img")],
    [InlineKeyboardButton("👤 Подготовить спикера", callback_data="menu:prep_speaker")],
    [InlineKeyboardButton("ℹ️ Помощь", callback_data="menu:help")],
])


async def _edit_picked(q, title: str, value_label: str) -> None:
    """Заменяет inline-клавиатуру строкой 'title: value' (визуально подтверждаем выбор)."""
    try:
        await q.edit_message_text(f"{title}: <b>{value_label}</b>", parse_mode="HTML")
    except Exception:
        # если не вышло отредактировать — не критично
        pass


# ── helpers: structured logging context ────────────────────────────────────
def _ulog(update: Update, action: str):
    """logger.bind с uid/username/action для удобной фильтрации логов по пользователю."""
    u = update.effective_user
    return logger.bind(
        uid=u.id if u else 0,
        uname=(u.username or u.full_name or "?") if u else "?",
        action=action,
    )


def _first_message(update: Update) -> Message:
    """Достаёт «сообщение, на которое отвечаем». Работает и для CommandHandler (update.message),
    и для CallbackQueryHandler (update.callback_query.message)."""
    if update.message is not None:
        return update.message
    if update.callback_query is not None and update.callback_query.message is not None:
        return update.callback_query.message
    raise RuntimeError("no message in update")


# ── helper для entry-точек: блокируем если очередь юзера полна ─────────────
async def _check_user_capacity(update: Update) -> bool:
    """Возвращает True если можно стартовать. Иначе шлёт отбивку и False.
    Работает и из CommandHandler и из CallbackQueryHandler."""
    state = get_state()
    uid = update.effective_user.id
    inflight, queued = state.user_load(uid)
    if inflight + queued >= USER_QUEUE_LIMIT:
        msg = (
            f"⏳ Очередь заполнена ({USER_QUEUE_LIMIT} задач, "
            f"в работе {inflight} / ожидают {queued}). Дождись пока что-то закончится."
        )
        target = _first_message(update)
        await target.reply_text(msg)
        return False
    return True


# ── enqueue: вызывается из финального хэндлера пикера ─────────────────────
async def _enqueue_task(
    *,
    ctx: ContextTypes.DEFAULT_TYPE,
    uid: int,
    uname: str,
    chat: Chat,
    runner: Callable[[], Awaitable[GenerationJob]],
    label: str,
    eta_sec: int | None = None,
    cleanup_paths: list[Path] | None = None,
    recipe_template: TaskRecipe | None = None,
) -> None:
    """Кладёт задачу в per-user очередь. Сразу шлёт пользователю статус с позицией.
    Сам пуск идёт в воркере state._user_worker (см. bot/state.py).

    Если передан recipe_template — это «заготовка» рецепта (без `task_uid`/`result_path`),
    которая будет дополнена в `_execute_task` после успешной отправки результата.
    """
    state = get_state()
    log = logger.bind(uid=uid, uname=uname, label=label)
    eta_hint = f" (обычно {_fmt_eta(eta_sec)})" if eta_sec else ""

    inflight, queued = state.user_load(uid)
    total_after = inflight + queued + 1
    if total_after > USER_QUEUE_LIMIT:
        log.warning(f"rejected: user queue full ({inflight} inflight + {queued} queued)")
        await chat.send_message(
            f"⏳ Очередь заполнена ({USER_QUEUE_LIMIT} задач). "
            f"Дождись пока что-то закончится."
        )
        return

    queue_pos = inflight + queued + 1  # 1-based «эта задача — N-я твоя в полёте»
    status: Message = await chat.send_message(
        f"📥 {label}: принято (твоя задача #{queue_pos}){eta_hint}"
    )
    queued_at = time.monotonic()
    task_uid = uuid.uuid4().hex[:8]

    async def task_lane() -> None:
        await _execute_task(
            ctx=ctx, uid=uid, uname=uname, chat=chat,
            runner=runner, label=label, eta_sec=eta_sec,
            status=status, queued_at=queued_at, task_uid=task_uid,
            cleanup_paths=cleanup_paths or [],
            recipe_template=recipe_template,
        )

    accepted = await state.submit_task(uid, task_lane)
    if not accepted:
        # Между user_load и submit_task проскочила другая задача — отбиваемся.
        log.warning("rejected at submit: queue full race")
        await status.edit_text(
            f"❌ Очередь заполнилась прямо сейчас, попробуй ещё раз."
        )
        return
    log.info(f"enqueued (queue_pos={queue_pos}, inflight={inflight}, queued={queued})")


async def _execute_task(
    *,
    ctx: ContextTypes.DEFAULT_TYPE,
    uid: int,
    uname: str,
    chat: Chat,
    runner: Callable[[], Awaitable[GenerationJob]],
    label: str,
    eta_sec: int | None,
    status: Message,
    queued_at: float,
    task_uid: str,
    cleanup_paths: list[Path],
    recipe_template: TaskRecipe | None,
) -> None:
    """Реально запускает задачу: ждёт глобальный семафор, дёргает runner, шлёт результат.
    На успехе сохраняет recipe + первую result-картинку в regen_cache для пост-задачных кнопок."""
    state = get_state()
    log = logger.bind(uid=uid, uname=uname, label=label, task=task_uid)
    eta_hint = f" (обычно {_fmt_eta(eta_sec)})" if eta_sec else ""

    try:
        await status.edit_text(f"📥 {label}: в очереди…{eta_hint}")
    except Exception:
        pass

    try:
        async with state.global_sem:
            queue_wait = time.monotonic() - queued_at
            log.info(f"started (queue_wait={queue_wait:.1f}s)")
            try:
                await status.edit_text(f"🎨 {label}: генерирую…{eta_hint}")
            except Exception:
                pass
            run_started = time.monotonic()
            job = await runner()
            gen_dur = time.monotonic() - run_started

        log = log.bind(job_id=job.job_id)
        log.info(
            f"job result status={job.status} urls={len(job.result_urls)} dur={gen_dur:.1f}s"
        )

        if job.status == "completed" and job.result_urls:
            await status.edit_text(
                f"✅ {label}: готово за {gen_dur:.0f}с (job_id={job.job_id})"
            )
            # Скачиваем + отправляем каждую картинку; ловим путь к первой для regen-cache.
            first_result_local: Path | None = None
            for i, url in enumerate(job.result_urls, 1):
                local = await _send_result_image(
                    chat=chat, log=log, url=url, uid=uid, task_uid=task_uid,
                    idx=i, total=len(job.result_urls),
                    with_action_kb=(i == 1 and recipe_template is not None),
                    action_task_uid=task_uid,
                )
                if i == 1 and local is not None:
                    first_result_local = local

            # Сохраняем recipe (если был template) — после отправки картинки.
            if recipe_template is not None:
                try:
                    _persist_recipe(
                        state=state, template=recipe_template, task_uid=task_uid,
                        cleanup_paths=cleanup_paths, first_result=first_result_local, log=log,
                    )
                except Exception as e:
                    log.opt(exception=e).warning(f"recipe save failed: {e!r}")
        elif job.status == "completed":
            log.error("completed but result_urls is empty")
            await status.edit_text(
                f"❌ {label}: задача завершилась, но Phygital не вернул ссылки на файлы"
            )
        else:
            log.warning(f"job not completed: {job.status} err={job.error!r}")
            await status.edit_text(f"❌ {label}: {job.status}\n{job.error or '—'}")
    except Exception as e:
        log.opt(exception=e).error(f"task crashed: {type(e).__name__}: {e}")
        try:
            await status.edit_text(f"❌ {label}: {type(e).__name__}: {e}")
        except Exception as e2:
            log.warning(f"could not edit status message: {e2!r}")
    finally:
        # Чистим только эти init-файлы (они уже не нужны: ушли в Phygital + скопированы в regen_cache).
        for p in cleanup_paths:
            try:
                if p.exists():
                    p.unlink()
            except Exception as e:
                log.debug(f"cleanup unlink {p}: {e}")
        state.clear_task_tmp(uid, task_uid)
        log.info("task done")


def _persist_recipe(
    *,
    state,
    template: TaskRecipe,
    task_uid: str,
    cleanup_paths: list[Path],
    first_result: Path | None,
    log,
) -> None:
    """Завершает заготовку recipe и кладёт её в state.recipes.
    Копирует init-файлы (cleanup_paths) в regen_dir, чтобы они пережили cleanup."""
    regen_dir = state.regen_dir(template.user_id, task_uid)
    cached_inits: list[Path] = []
    for i, p in enumerate(cleanup_paths, 1):
        if not p.exists():
            continue
        suffix = p.suffix or ".jpg"
        dst = regen_dir / f"init_{i}{suffix}"
        try:
            shutil.copyfile(p, dst)
            cached_inits.append(dst)
        except Exception as e:
            log.debug(f"copy init {p} → regen failed: {e}")

    recipe = dataclasses.replace(
        template,
        task_uid=task_uid,
        init_paths=cached_inits,
        result_path=first_result,
    )
    state.save_recipe(recipe)
    log.info(
        f"recipe saved: workflow={recipe.workflow} inits={len(cached_inits)} "
        f"has_result={first_result is not None}"
    )


async def _send_result_image(
    *, chat: Chat, log, url: str, uid: int, task_uid: str, idx: int, total: int,
    with_action_kb: bool = False, action_task_uid: str | None = None,
) -> Path | None:
    """Скачивает result-картинку и отправляет в чат. Возвращает локальный путь (для regen_cache).

    Стратегия:
      1) Сначала пробуем `chat.send_photo(url)` — пусть Telegram сам тянет с S3.
         (быстрее, без нашей упаковки multipart, без локального диска до фейла).
      2) При любом фейле — качаем сами в `regen_dir(uid, task_uid)` и отдаём InputFile
         (photo до 10MB, иначе document).

    Если `with_action_kb=True` — прикрепляем `_action_keyboard(action_task_uid)` к сообщению.
    Кнопки появляются и при url-send и при upload-fallback.
    Если в обоих случаях задеплоить кнопки не получилось (не послали картинку вообще) —
    шлём ссылку отдельным сообщением, кнопки крепим к нему.

    Returns:
      Path к скачанному файлу в regen_cache, если успели скачать. None — если отправили
      исключительно через URL без локального копирования (regen без кэша file-init не сломается,
      потому что у нас уже есть url… но для image2image это критично — см. as_i2i_callback).
      Поэтому при `with_action_kb=True` мы всегда скачиваем (см. ниже).
    """
    state = get_state()
    kb = _action_keyboard(action_task_uid) if (with_action_kb and action_task_uid) else None

    # Если нужны кнопки + локальный путь (для asi2i) — пропускаем url-стратегию,
    # сразу качаем и шлём upload. Иначе оптимизируем под скорость и пробуем url первым.
    if not with_action_kb:
        try:
            await chat.send_photo(url)
            log.info(f"sent {idx}/{total} via url")
            return None
        except Exception as e:
            log.warning(
                f"send_photo(url) failed for {idx}/{total}: "
                f"{type(e).__name__}: {e}; downloading and retrying as upload"
            )

    regen_dir = state.regen_dir(uid, task_uid)
    local = regen_dir / f"result_{idx}.png"
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True, verify=_SSL_CTX) as cli:
            r = await cli.get(url)
            r.raise_for_status()
            local.write_bytes(r.content)
        size = local.stat().st_size
        log.info(
            f"downloaded {idx}/{total} → {local.name} "
            f"({size / 1024 / 1024:.2f}MB) — sending as upload"
        )
        with local.open("rb") as fh:
            inp = InputFile(fh, filename=local.name)
            if size > TG_PHOTO_LIMIT_BYTES:
                await chat.send_document(document=inp, reply_markup=kb)
                log.info(f"sent {idx}/{total} as document (size>10MB)")
            else:
                await chat.send_photo(photo=inp, reply_markup=kb)
                log.info(f"sent {idx}/{total} as photo upload")
        return local
    except Exception as e:
        log.opt(exception=e).error(
            f"upload fallback failed for {idx}/{total}: {type(e).__name__}: {e}"
        )
        try:
            await chat.send_message(
                f"❌ не смог отправить картинку {idx}/{total}, ссылка:\n{url}",
                reply_markup=kb,
            )
        except Exception as e2:
            log.warning(f"send_message(url) also failed: {e2!r}")
        return None


# ── /cancel — общий выход из любого сценария ───────────────────────────────
@whitelist_only
async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Отменяет текущий пикер. Активные задачи в Phygital не трогает — там уже
    идёт работа, прерывание ничего не сэкономит. Очищаем только UI-state."""
    _ulog(update, "/cancel").info("user cancelled picker")
    ctx.user_data.clear()
    # Снимаем pending-edit если был (юзер передумал писать новый промпт).
    state = get_state()
    state.clear_pending_edit(update.effective_user.id)
    if update.callback_query:
        try:
            await update.callback_query.answer("Отменено.")
        except Exception:
            pass
        try:
            await update.callback_query.edit_message_text("✖️ Отменено.")
        except Exception:
            pass
    elif update.message:
        await update.message.reply_text("Отменено (текущие задачи в Phygital продолжают идти).")
    return ConversationHandler.END


# ── GPT Image: первая ступень пикера после выбора ноды ────────────────────
async def _ask_gpt_quality(q) -> None:
    await q.answer()
    await _edit_picked(q, "Модель", "GPT Image 2")
    await q.message.chat.send_message(
        "Качество (стоимость растёт с High):",
        reply_markup=_kb("gptq", GPT_QUALITIES, cols=3),
    )


# ── /generate ──────────────────────────────────────────────────────────────
@whitelist_only
async def gen_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _check_user_capacity(update):
        return ConversationHandler.END
    log = _ulog(update, "/generate")
    # Если зашли через menu-callback — подтверждаем нажатие.
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass
    target = _first_message(update)
    # `/generate <prompt>` поддерживается только когда вход через CommandHandler.
    args = (ctx.args or []) if update.message else []
    if args:
        prompt = " ".join(args).strip()
        ctx.user_data["prompt"] = prompt
        log.info(f"inline args: prompt_chars={len(prompt)} — asking for node")
        await target.reply_text(
            f"Выбери ноду:\n"
            f"• Nano Banana — {_fmt_eta(ETA_NANO_BANANA_SEC)} на картинку\n"
            f"• GPT Image 2 — {_fmt_eta(ETA_GPT_IMAGE_SEC)} на картинку",
            reply_markup=_kb("node", NODES, cols=2),
        )
        return GEN_NODE
    log.info("entered prompt-collection state")
    await target.reply_text("✍️ Пришли текст промпта (или /cancel):")
    return GEN_PROMPT


@whitelist_only
async def gen_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    log = _ulog(update, "/generate")
    prompt = (update.message.text or "").strip()
    if not prompt:
        log.warning("empty prompt, asking again")
        await update.message.reply_text("Пустой prompt. Попробуй ещё раз или /cancel.")
        return GEN_PROMPT
    ctx.user_data["prompt"] = prompt
    log.info(f"prompt received: chars={len(prompt)}; asking for node")
    await update.message.reply_text(
        f"Выбери ноду:\n"
        f"• Nano Banana — {_fmt_eta(ETA_NANO_BANANA_SEC)} на картинку\n"
        f"• GPT Image 2 — {_fmt_eta(ETA_GPT_IMAGE_SEC)} на картинку",
        reply_markup=_kb("node", NODES, cols=2),
    )
    return GEN_NODE


@whitelist_only
async def gen_node(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    node = q.data.split(":", 1)[1]
    log = _ulog(update, "/generate")
    ctx.user_data["node"] = node
    log.info(f"node picked: {node}")
    if node == "gpt":
        await _ask_gpt_quality(q)
        return GEN_GPT_QUALITY
    await q.answer()
    await _edit_picked(q, "Модель", "Nano Banana")
    await q.message.chat.send_message(
        "Какой вариант модели?", reply_markup=_kb("model", GEMINI_MODELS, cols=2)
    )
    return GEN_MODEL


@whitelist_only
async def gen_gpt_quality(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    quality = q.data.split(":", 1)[1]
    ctx.user_data["gpt_quality"] = quality
    _ulog(update, "/generate").info(f"gpt quality: {quality}")
    await _edit_picked(q, "Качество", quality)
    await q.message.chat.send_message(
        "Соотношение сторон / разрешение:",
        reply_markup=_kb("gpta", GPT_ASPECTS, cols=3),
    )
    return GEN_GPT_ASPECT


@whitelist_only
async def gen_gpt_aspect(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    aspect = q.data.split(":", 1)[1]
    ctx.user_data["gpt_aspect"] = aspect
    label = dict(GPT_ASPECTS).get(aspect, aspect)
    _ulog(update, "/generate").info(f"gpt aspect: {aspect}")
    await _edit_picked(q, "Размер", label)
    await q.message.chat.send_message(
        "Фон:",
        reply_markup=_kb("gptb", GPT_BACKGROUNDS, cols=3),
    )
    return GEN_GPT_BG


@whitelist_only
async def gen_gpt_bg(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    bg = q.data.split(":", 1)[1]
    ctx.user_data["gpt_bg"] = bg
    _ulog(update, "/generate").info(f"gpt background: {bg}; launching")
    await _edit_picked(q, "Фон", bg)
    await _gen_run_gpt(update, ctx)
    return ConversationHandler.END


async def _gen_run_gpt(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    prompt: str = ctx.user_data["prompt"]
    quality = ctx.user_data.get("gpt_quality", "Medium")
    aspect = ctx.user_data.get("gpt_aspect", "auto")
    bg = ctx.user_data.get("gpt_bg", "auto")
    state = get_state()
    u = update.effective_user
    chat = update.effective_chat

    async def runner() -> GenerationJob:
        async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
            wf = GPTImageWorkflow(
                client, version="v2", aspect_ratio=aspect,
                quality=quality, background=bg, number_of_images=1,
            )
            return await wf.run(prompt=prompt)

    template = TaskRecipe(
        task_uid="",  # будет проставлен в _execute_task
        user_id=u.id,
        label=f"generate-gpt/{quality}/{aspect}/{bg}",
        workflow="gpt_t2i",
        prompt=prompt,
        params={"quality": quality, "aspect": aspect, "bg": bg},
    )
    await _enqueue_task(
        ctx=ctx,
        uid=u.id,
        uname=u.username or u.full_name or "?",
        chat=chat,
        runner=runner,
        label=f"generate-gpt/{quality}/{aspect}/{bg}",
        eta_sec=ETA_GPT_IMAGE_SEC,
        recipe_template=template,
    )


@whitelist_only
async def gen_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    model = q.data.split(":", 1)[1]
    ctx.user_data["model_name"] = model
    label = dict(GEMINI_MODELS).get(model, model)
    _ulog(update, "/generate").info(f"model_name: {model}")
    await _edit_picked(q, "Вариант", label)
    await q.message.chat.send_message(
        "Соотношение сторон:", reply_markup=_kb("ratio", GEMINI_RATIOS, cols=4)
    )
    return GEN_RATIO


@whitelist_only
async def gen_ratio(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    ratio = q.data.split(":", 1)[1]
    ctx.user_data["ratio"] = ratio
    label = dict(GEMINI_RATIOS).get(ratio, ratio)
    _ulog(update, "/generate").info(f"ratio: {ratio}")
    await _edit_picked(q, "Соотношение", label)
    await q.message.chat.send_message(
        "Разрешение:", reply_markup=_kb("res", GEMINI_RESOLUTIONS, cols=4)
    )
    return GEN_RES


@whitelist_only
async def gen_res(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    resolution = q.data.split(":", 1)[1]
    ctx.user_data["resolution"] = resolution
    label = dict(GEMINI_RESOLUTIONS).get(resolution, resolution)
    _ulog(update, "/generate").info(f"resolution: {resolution}; launching")
    await _edit_picked(q, "Разрешение", label)
    await _gen_run(update, ctx)
    return ConversationHandler.END


async def _gen_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    prompt: str = ctx.user_data["prompt"]
    model = ctx.user_data.get("model_name", "v3")
    ratio = ctx.user_data.get("ratio", "default")
    resolution = ctx.user_data.get("resolution", "default")
    state = get_state()
    u = update.effective_user
    chat = update.effective_chat

    async def runner() -> GenerationJob:
        async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
            wf = ImageGenWorkflow(
                client, model_name=model, ratio=ratio, resolution=resolution
            )
            return await wf.run(prompt=prompt)

    template = TaskRecipe(
        task_uid="", user_id=u.id,
        label=f"generate/{model}/{ratio}/{resolution}",
        workflow="nb_t2i", prompt=prompt,
        params={"model": model, "ratio": ratio, "resolution": resolution},
    )
    await _enqueue_task(
        ctx=ctx,
        uid=u.id,
        uname=u.username or u.full_name or "?",
        chat=chat,
        runner=runner,
        label=f"generate/{model}/{ratio}/{resolution}",
        eta_sec=ETA_NANO_BANANA_SEC,
        recipe_template=template,
    )


# ── /img2img ───────────────────────────────────────────────────────────────
@whitelist_only
async def i2i_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _check_user_capacity(update):
        return ConversationHandler.END
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass
    _ulog(update, "/img2img").info("started")
    ctx.user_data["init_paths"] = []
    target = _first_message(update)
    await target.reply_text(
        "🖼 Пришли 1–4 init-изображения (фото или файлом), потом /done.\n"
        "/cancel — отменить."
    )
    return I2I_COLLECT


@whitelist_only
async def i2i_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    log = _ulog(update, "/img2img")
    path = await _download_image(update, ctx, log)
    if not path:
        log.warning("non-image payload in I2I_COLLECT")
        await update.message.reply_text("⚠️ Это не картинка. Пришли photo или image-document.")
        return I2I_COLLECT
    ctx.user_data["init_paths"].append(path)
    n = len(ctx.user_data["init_paths"])
    log.info(f"collected init image #{n}: {path.name}")
    await update.message.reply_text(f"📥 принято {n}. Ещё или /done.")
    return I2I_COLLECT


@whitelist_only
async def i2i_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    log = _ulog(update, "/img2img")
    n = len(ctx.user_data.get("init_paths") or [])
    if not n:
        log.warning("/done before any image")
        await update.message.reply_text("Сначала пришли хотя бы одну картинку.")
        return I2I_COLLECT
    log.info(f"done collecting, total={n}; waiting for prompt")
    await update.message.reply_text("✍️ Теперь текст промпта (или /cancel):")
    return I2I_PROMPT


@whitelist_only
async def i2i_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    log = _ulog(update, "/img2img")
    prompt = (update.message.text or "").strip()
    if not prompt:
        log.warning("empty prompt for img2img")
        await update.message.reply_text("Пустой prompt. Попробуй ещё раз или /cancel.")
        return I2I_PROMPT
    ctx.user_data["prompt"] = prompt
    log.info(f"prompt received: chars={len(prompt)}; asking for node")
    await update.message.reply_text(
        f"Выбери ноду:\n"
        f"• Nano Banana — {_fmt_eta(ETA_NANO_BANANA_SEC)} на картинку\n"
        f"• GPT Image 2 — {_fmt_eta(ETA_GPT_IMAGE_SEC)} на картинку",
        reply_markup=_kb("node", NODES, cols=2),
    )
    return I2I_NODE


@whitelist_only
async def i2i_node(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    node = q.data.split(":", 1)[1]
    log = _ulog(update, "/img2img")
    ctx.user_data["node"] = node
    log.info(f"node picked: {node}")
    if node == "gpt":
        await _ask_gpt_quality(q)
        return I2I_GPT_QUALITY
    await q.answer()
    await _edit_picked(q, "Модель", "Nano Banana")
    await q.message.chat.send_message(
        "Какой вариант модели?", reply_markup=_kb("model", GEMINI_MODELS, cols=2)
    )
    return I2I_MODEL


@whitelist_only
async def i2i_gpt_quality(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    quality = q.data.split(":", 1)[1]
    ctx.user_data["gpt_quality"] = quality
    _ulog(update, "/img2img").info(f"gpt quality: {quality}")
    await _edit_picked(q, "Качество", quality)
    await q.message.chat.send_message(
        "Соотношение сторон / разрешение:",
        reply_markup=_kb("gpta", GPT_ASPECTS, cols=3),
    )
    return I2I_GPT_ASPECT


@whitelist_only
async def i2i_gpt_aspect(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    aspect = q.data.split(":", 1)[1]
    ctx.user_data["gpt_aspect"] = aspect
    label = dict(GPT_ASPECTS).get(aspect, aspect)
    _ulog(update, "/img2img").info(f"gpt aspect: {aspect}")
    await _edit_picked(q, "Размер", label)
    await q.message.chat.send_message(
        "Фон:",
        reply_markup=_kb("gptb", GPT_BACKGROUNDS, cols=3),
    )
    return I2I_GPT_BG


@whitelist_only
async def i2i_gpt_bg(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    bg = q.data.split(":", 1)[1]
    ctx.user_data["gpt_bg"] = bg
    log = _ulog(update, "/img2img")
    log.info(f"gpt background: {bg}; launching")
    await _edit_picked(q, "Фон", bg)

    prompt: str = ctx.user_data["prompt"]
    init_paths: list[Path] = ctx.user_data["init_paths"]
    quality = ctx.user_data.get("gpt_quality", "Medium")
    aspect = ctx.user_data.get("gpt_aspect", "auto")
    state = get_state()
    u = update.effective_user
    chat = update.effective_chat

    async def runner() -> GenerationJob:
        async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
            wf = GPTImageWorkflow(
                client, version="v2", aspect_ratio=aspect,
                quality=quality, background=bg, number_of_images=1,
            )
            return await wf.run_with_files(prompt=prompt, init_paths=init_paths)

    template = TaskRecipe(
        task_uid="", user_id=u.id,
        label=f"img2img-gpt/{quality}/{aspect}/{bg}",
        workflow="gpt_i2i", prompt=prompt,
        params={"quality": quality, "aspect": aspect, "bg": bg},
    )
    await _enqueue_task(
        ctx=ctx,
        uid=u.id,
        uname=u.username or u.full_name or "?",
        chat=chat,
        runner=runner,
        label=f"img2img-gpt/{quality}/{aspect}/{bg}",
        eta_sec=ETA_GPT_IMAGE_SEC,
        cleanup_paths=list(init_paths),
        recipe_template=template,
    )
    return ConversationHandler.END


@whitelist_only
async def i2i_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    model = q.data.split(":", 1)[1]
    ctx.user_data["model_name"] = model
    label = dict(GEMINI_MODELS).get(model, model)
    _ulog(update, "/img2img").info(f"model_name: {model}")
    await _edit_picked(q, "Вариант", label)
    await q.message.chat.send_message(
        "Соотношение сторон:", reply_markup=_kb("ratio", GEMINI_RATIOS, cols=4)
    )
    return I2I_RATIO


@whitelist_only
async def i2i_ratio(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    ratio = q.data.split(":", 1)[1]
    ctx.user_data["ratio"] = ratio
    label = dict(GEMINI_RATIOS).get(ratio, ratio)
    _ulog(update, "/img2img").info(f"ratio: {ratio}")
    await _edit_picked(q, "Соотношение", label)
    await q.message.chat.send_message(
        "Разрешение:", reply_markup=_kb("res", GEMINI_RESOLUTIONS, cols=4)
    )
    return I2I_RES


@whitelist_only
async def i2i_res(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    resolution = q.data.split(":", 1)[1]
    ctx.user_data["resolution"] = resolution
    label = dict(GEMINI_RESOLUTIONS).get(resolution, resolution)
    log = _ulog(update, "/img2img")
    log.info(f"resolution: {resolution}; launching")
    await _edit_picked(q, "Разрешение", label)

    prompt = ctx.user_data["prompt"]
    init_paths: list[Path] = ctx.user_data["init_paths"]
    model = ctx.user_data.get("model_name", "v3_1")
    ratio = ctx.user_data.get("ratio", "r_3_4")
    state = get_state()
    u = update.effective_user
    chat = update.effective_chat

    async def runner() -> GenerationJob:
        async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
            wf = ImageToImageWorkflow(
                client, model_name=model, ratio=ratio, resolution=resolution
            )
            return await wf.run_with_files(prompt=prompt, init_paths=init_paths)

    template = TaskRecipe(
        task_uid="", user_id=u.id,
        label=f"img2img/{model}/{ratio}/{resolution}",
        workflow="nb_i2i", prompt=prompt,
        params={"model": model, "ratio": ratio, "resolution": resolution},
    )
    await _enqueue_task(
        ctx=ctx,
        uid=u.id,
        uname=u.username or u.full_name or "?",
        chat=chat,
        runner=runner,
        label=f"img2img/{model}/{ratio}/{resolution}",
        eta_sec=ETA_NANO_BANANA_SEC,
        cleanup_paths=list(init_paths),
        recipe_template=template,
    )
    return ConversationHandler.END


# ── /prep_speaker ──────────────────────────────────────────────────────────
# Модель фиксирована (v3.1) — промпты заточены под неё. Спрашиваем gender,
# ratio (по умолчанию 3:4) и resolution (по умолчанию 2K).
@whitelist_only
async def sp_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _check_user_capacity(update):
        return ConversationHandler.END
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass
    _ulog(update, "/prep_speaker").info("started")
    ctx.user_data["speaker_photo"] = None
    target = _first_message(update)
    await target.reply_text(
        "👤 Пришли фото спикера (photo или файлом).\n/cancel — отменить."
    )
    return SP_PHOTO


@whitelist_only
async def sp_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    log = _ulog(update, "/prep_speaker")
    path = await _download_image(update, ctx, log)
    if not path:
        log.warning("non-image payload in SP_PHOTO")
        await update.message.reply_text("⚠️ Это не картинка. Пришли фото.")
        return SP_PHOTO
    ctx.user_data["speaker_photo"] = path
    log.info(f"speaker photo saved: {path.name}")
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👨 Мужчина", callback_data="gender:man"),
            InlineKeyboardButton("👩 Женщина", callback_data="gender:woman"),
        ],
        [InlineKeyboardButton("✖️ Отмена", callback_data="cancel:picker")],
    ])
    await update.message.reply_text("Кто на фото?", reply_markup=kb)
    return SP_GENDER


@whitelist_only
async def sp_gender(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    gender = q.data.split(":", 1)[1]  # man | woman
    ctx.user_data["gender"] = gender
    _ulog(update, "/prep_speaker").info(f"gender: {gender}")
    await _edit_picked(q, "Пол", "Мужчина" if gender == "man" else "Женщина")
    await q.message.chat.send_message(
        "Соотношение сторон (рекомендовано 3:4):",
        reply_markup=_kb("ratio", GEMINI_RATIOS, cols=4),
    )
    return SP_RATIO


@whitelist_only
async def sp_ratio(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    ratio = q.data.split(":", 1)[1]
    ctx.user_data["ratio"] = ratio
    label = dict(GEMINI_RATIOS).get(ratio, ratio)
    _ulog(update, "/prep_speaker").info(f"ratio: {ratio}")
    await _edit_picked(q, "Соотношение", label)
    await q.message.chat.send_message(
        "Разрешение (рекомендовано 2K):",
        reply_markup=_kb("res", GEMINI_RESOLUTIONS, cols=4),
    )
    return SP_RES


@whitelist_only
async def sp_res(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    resolution = q.data.split(":", 1)[1]
    ctx.user_data["resolution"] = resolution
    label = dict(GEMINI_RESOLUTIONS).get(resolution, resolution)
    log = _ulog(update, "/prep_speaker")
    log.info(f"resolution: {resolution}; launching")
    await _edit_picked(q, "Разрешение", label)

    gender = ctx.user_data["gender"]
    photo: Path = ctx.user_data["speaker_photo"]
    ratio = ctx.user_data.get("ratio", "r_3_4")
    state = get_state()
    prompt = speaker_prompt(gender)  # type: ignore[arg-type]
    u = update.effective_user
    chat = update.effective_chat

    async def runner() -> GenerationJob:
        async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
            wf = build_speaker_prep_workflow(client)
            # Перебиваем дефолтные параметры выбором пользователя.
            wf.ratio = ratio
            wf.resolution = resolution
            return await wf.run_with_files(
                prompt=prompt, init_paths=[DEFAULT_REFERENCE, photo]
            )

    template = TaskRecipe(
        task_uid="", user_id=u.id,
        label=f"prep-speaker/{gender}/{ratio}/{resolution}",
        workflow="speaker", prompt=prompt,
        params={"gender": gender, "ratio": ratio, "resolution": resolution},
    )
    # cleanup_paths — только фото юзера; DEFAULT_REFERENCE — постоянный ассет, не удаляем.
    await _enqueue_task(
        ctx=ctx,
        uid=u.id,
        uname=u.username or u.full_name or "?",
        chat=chat,
        runner=runner,
        label=f"prep-speaker/{gender}/{ratio}/{resolution}",
        eta_sec=ETA_NANO_BANANA_SEC,
        cleanup_paths=[photo],
        recipe_template=template,
    )
    return ConversationHandler.END


# ── helpers ────────────────────────────────────────────────────────────────
async def _download_image(update: Update, ctx: ContextTypes.DEFAULT_TYPE, log) -> Path | None:
    """Скачивает фото или image-document от пользователя во временную папку и возвращает путь."""
    state = get_state()
    uid = update.effective_user.id
    msg = update.message
    file_id: str | None = None
    suffix = ".jpg"
    declared_size: int | None = None

    if msg.photo:
        ph = msg.photo[-1]  # самый большой размер
        file_id = ph.file_id
        declared_size = ph.file_size
        suffix = ".jpg"
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        file_id = msg.document.file_id
        declared_size = msg.document.file_size
        fname = msg.document.file_name or "img"
        suffix = Path(fname).suffix or ".jpg"

    if not file_id:
        return None

    log.info(
        f"downloading image file_id={file_id[:16]}… "
        f"declared_size={declared_size or '?'} suffix={suffix}"
    )
    tg_file = await ctx.bot.get_file(file_id)
    tmp = state.user_tmp(uid)
    out = tmp / f"{int(time.time() * 1000)}{suffix}"
    await tg_file.download_to_drive(custom_path=out)
    log.info(f"downloaded → {out} ({out.stat().st_size / 1024:.0f}KB)")
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Пост-задачные действия: 🔄 Повторить / ✏️ Уточнить / 🖼 Как img2img + меню
# ═══════════════════════════════════════════════════════════════════════════

@whitelist_only
async def cmd_menu(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать главное меню inline-кнопками."""
    _ulog(update, "/menu").info("menu opened")
    await update.message.reply_text("Меню:", reply_markup=MENU_KEYBOARD)


@whitelist_only
async def menu_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Роутер callback-кнопок главного меню. menu:help отрабатываем сами.
    menu:generate/img2img/prep_speaker отрабатываются как entry_points соответствующих
    ConversationHandler — этот хендлер для них не вызывается."""
    q = update.callback_query
    action = q.data.split(":", 1)[1]
    await q.answer()
    if action == "help":
        # Импорт из main избежим, текст всё равно дублируется в /help-хендлере.
        from bot.main import HELP_TEXT  # noqa: WPS433  локальный импорт чтобы не цикл
        await q.message.reply_text(HELP_TEXT)


async def _check_user_capacity_cb(update: Update) -> bool:
    """Версия _check_user_capacity для CallbackQuery-входа — без падения если update.message=None."""
    return await _check_user_capacity(update)


@whitelist_only
async def regen_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """🔄 Повторить — повторно запускает задачу с теми же параметрами и тем же промптом."""
    q = update.callback_query
    task_uid = q.data.split(":", 1)[1]
    state = get_state()
    recipe = state.get_recipe(task_uid)
    if recipe is None:
        await q.answer("⚠️ Параметры этой задачи устарели (>24ч).")
        return
    if not await _check_user_capacity_cb(update):
        await q.answer()
        return
    await q.answer("🔄 Повторяю с теми же параметрами…")
    _ulog(update, "regen").info(f"from task={task_uid} workflow={recipe.workflow}")
    await _rerun_from_recipe(ctx, update, recipe, prompt_override=None)


@whitelist_only
async def edit_prompt_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """✏️ Уточнить — присылает текущий промпт в `<pre>` для удобного копирования и ждёт новый."""
    q = update.callback_query
    task_uid = q.data.split(":", 1)[1]
    state = get_state()
    recipe = state.get_recipe(task_uid)
    if recipe is None:
        await q.answer("⚠️ Параметры этой задачи устарели (>24ч).")
        return
    await q.answer()
    uid = update.effective_user.id
    state.set_pending_edit(uid, task_uid)
    _ulog(update, "edit-prompt").info(f"pending edit set for task={task_uid}")
    safe = html.escape(recipe.prompt)
    await q.message.reply_text(
        "✏️ Текущий промпт (скопируй из код-блока, отредактируй и отправь следующим сообщением):\n\n"
        f"<pre>{safe}</pre>\n\n"
        "Жду новый промпт ~5 минут. /cancel — отменить.\n"
        "⚠️ Если ты сейчас в активном сценарии (/generate, /img2img, /prep_speaker) — "
        "сначала /cancel, иначе твой текст уйдёт туда.",
        parse_mode="HTML",
    )


@whitelist_only
async def pending_edit_listener(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный MessageHandler. Срабатывает на любое текстовое сообщение пользователя
    ВНЕ активного ConversationHandler — потому что conversations забирают input первыми.
    Если у юзера есть свежий pending-edit — берём текст как новый промпт."""
    state = get_state()
    uid = update.effective_user.id
    if not state.has_pending_edit(uid):
        return
    task_uid = state.pop_pending_edit(uid)
    if task_uid is None:
        return
    recipe = state.get_recipe(task_uid)
    if recipe is None:
        await update.message.reply_text(
            "⚠️ Параметры исходной задачи устарели — повтор недоступен."
        )
        return
    new_prompt = (update.message.text or "").strip()
    if not new_prompt:
        await update.message.reply_text("⚠️ Пустой промпт, отменено.")
        return
    if not await _check_user_capacity(update):
        return
    _ulog(update, "edit-prompt").info(
        f"new prompt for task={task_uid} chars={len(new_prompt)}"
    )
    await _rerun_from_recipe(ctx, update, recipe, prompt_override=new_prompt)


@whitelist_only
async def as_i2i_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """🖼 Использовать результат как init для img2img — entry в conv_img2img."""
    q = update.callback_query
    task_uid = q.data.split(":", 1)[1]
    state = get_state()
    recipe = state.get_recipe(task_uid)
    if recipe is None or recipe.result_path is None or not recipe.result_path.exists():
        await q.answer("⚠️ Картинка устарела или не сохранилась локально.")
        return ConversationHandler.END
    if not await _check_user_capacity(update):
        await q.answer()
        return ConversationHandler.END
    await q.answer("Беру эту картинку как init.")

    uid = update.effective_user.id
    user_tmp = state.user_tmp(uid)
    suffix = recipe.result_path.suffix or ".png"
    dst = user_tmp / f"asi2i_{int(time.time() * 1000)}{suffix}"
    try:
        shutil.copyfile(recipe.result_path, dst)
    except Exception as e:
        _ulog(update, "asi2i").warning(f"copy failed: {e!r}")
        await q.message.reply_text("⚠️ Не смог подготовить картинку. Попробуй ещё раз.")
        return ConversationHandler.END

    ctx.user_data["init_paths"] = [dst]
    _ulog(update, "/img2img").info(f"asi2i: started from task={task_uid}, init={dst.name}")
    await q.message.reply_text(
        "🖼 Использую эту картинку как init.\n"
        "Можешь прислать ещё фото или сразу /done."
    )
    return I2I_COLLECT


# ── _rerun_from_recipe ─────────────────────────────────────────────────────
async def _rerun_from_recipe(
    ctx: ContextTypes.DEFAULT_TYPE,
    update: Update,
    recipe: TaskRecipe,
    prompt_override: str | None,
) -> None:
    """Собирает workflow по recipe.workflow и enqueue задачу.
    `prompt_override` — для ✏️ Уточнить (передаём новый промпт)."""
    state = get_state()
    u = update.effective_user
    chat = update.effective_chat
    prompt = prompt_override if prompt_override is not None else recipe.prompt
    params = recipe.params

    if recipe.workflow == "nb_t2i":
        model = params.get("model", "v3")
        ratio = params.get("ratio", "default")
        resolution = params.get("resolution", "default")

        async def runner() -> GenerationJob:
            async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
                wf = ImageGenWorkflow(client, model_name=model, ratio=ratio, resolution=resolution)
                return await wf.run(prompt=prompt)

        label = f"generate/{model}/{ratio}/{resolution}"
        eta = ETA_NANO_BANANA_SEC
        cleanup = []
        new_workflow = "nb_t2i"

    elif recipe.workflow == "gpt_t2i":
        quality = params.get("quality", "Medium")
        aspect = params.get("aspect", "auto")
        bg = params.get("bg", "auto")

        async def runner() -> GenerationJob:
            async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
                wf = GPTImageWorkflow(
                    client, version="v2", aspect_ratio=aspect,
                    quality=quality, background=bg, number_of_images=1,
                )
                return await wf.run(prompt=prompt)

        label = f"generate-gpt/{quality}/{aspect}/{bg}"
        eta = ETA_GPT_IMAGE_SEC
        cleanup = []
        new_workflow = "gpt_t2i"

    elif recipe.workflow == "nb_i2i":
        model = params.get("model", "v3_1")
        ratio = params.get("ratio", "r_3_4")
        resolution = params.get("resolution", "default")
        # Готовим init-пути в свежем task_tmp — нельзя пускать regen_cache напрямую,
        # потому что cleanup_paths их потом удалит после задачи.
        init_paths = _stage_inits_from_recipe(state, u.id, recipe)

        async def runner() -> GenerationJob:
            async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
                wf = ImageToImageWorkflow(client, model_name=model, ratio=ratio, resolution=resolution)
                return await wf.run_with_files(prompt=prompt, init_paths=init_paths)

        label = f"img2img/{model}/{ratio}/{resolution}"
        eta = ETA_NANO_BANANA_SEC
        cleanup = list(init_paths)
        new_workflow = "nb_i2i"

    elif recipe.workflow == "gpt_i2i":
        quality = params.get("quality", "Medium")
        aspect = params.get("aspect", "auto")
        bg = params.get("bg", "auto")
        init_paths = _stage_inits_from_recipe(state, u.id, recipe)

        async def runner() -> GenerationJob:
            async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
                wf = GPTImageWorkflow(
                    client, version="v2", aspect_ratio=aspect,
                    quality=quality, background=bg, number_of_images=1,
                )
                return await wf.run_with_files(prompt=prompt, init_paths=init_paths)

        label = f"img2img-gpt/{quality}/{aspect}/{bg}"
        eta = ETA_GPT_IMAGE_SEC
        cleanup = list(init_paths)
        new_workflow = "gpt_i2i"

    elif recipe.workflow == "speaker":
        gender = params.get("gender", "man")
        ratio = params.get("ratio", "r_3_4")
        resolution = params.get("resolution", "k2")
        # Спикер-фото — второй элемент init_paths (первый был DEFAULT_REFERENCE).
        # Но в recipe.init_paths мы сохранили только cleanup_paths, т.е. без DEFAULT_REFERENCE.
        init_paths = _stage_inits_from_recipe(state, u.id, recipe)
        if not init_paths:
            await chat.send_message("⚠️ Не нашёл локальной копии фото спикера для повтора.")
            return
        speaker_photo = init_paths[0]
        # Если ✏️ Уточнить вызвал regen — prompt в recipe собран из speaker_prompt(gender);
        # юзерский override применяем как есть (он же и редактирует этот системный промпт).

        async def runner() -> GenerationJob:
            async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
                wf = build_speaker_prep_workflow(client)
                wf.ratio = ratio
                wf.resolution = resolution
                return await wf.run_with_files(
                    prompt=prompt, init_paths=[DEFAULT_REFERENCE, speaker_photo]
                )

        label = f"prep-speaker/{gender}/{ratio}/{resolution}"
        eta = ETA_NANO_BANANA_SEC
        cleanup = [speaker_photo]
        new_workflow = "speaker"

    else:
        await chat.send_message(f"⚠️ Не знаю как повторить workflow={recipe.workflow}")
        return

    new_template = TaskRecipe(
        task_uid="", user_id=u.id,
        label=label + (" (edit)" if prompt_override else " (regen)"),
        workflow=new_workflow, prompt=prompt, params=params.copy(),
    )
    await _enqueue_task(
        ctx=ctx,
        uid=u.id,
        uname=u.username or u.full_name or "?",
        chat=chat,
        runner=runner,
        label=new_template.label,
        eta_sec=eta,
        cleanup_paths=cleanup,
        recipe_template=new_template,
    )


def _stage_inits_from_recipe(state, uid: int, recipe: TaskRecipe) -> list[Path]:
    """Копирует init-файлы из regen_cache в user_tmp (свежие имена), чтобы их можно было
    передать в workflow и потом безопасно удалить через cleanup_paths."""
    out: list[Path] = []
    user_tmp = state.user_tmp(uid)
    ts = int(time.time() * 1000)
    for i, src in enumerate(recipe.init_paths, 1):
        if not src.exists():
            continue
        dst = user_tmp / f"regen_{ts}_{i}{src.suffix or '.jpg'}"
        try:
            shutil.copyfile(src, dst)
            out.append(dst)
        except Exception:
            continue
    return out


# ── фабрика для регистрации в Application ──────────────────────────────────
def build_conversations() -> list[ConversationHandler]:
    """Три ConversationHandler — /generate, /img2img, /prep_speaker.
    Каждый entry-point дублируется: CommandHandler + CallbackQueryHandler для menu-кнопки.
    Img2img дополнительно слушает `asi2i:*` (Use as img2img на готовом результате).
    Fallbacks включают inline-Cancel (callback `cancel:*`) и /cancel."""
    img_filter = filters.PHOTO | (filters.Document.IMAGE)
    cancel_handlers = [
        CommandHandler("cancel", cmd_cancel),
        CallbackQueryHandler(cmd_cancel, pattern=r"^cancel:"),
    ]

    conv_generate = ConversationHandler(
        entry_points=[
            CommandHandler("generate", gen_start),
            CallbackQueryHandler(gen_start, pattern=r"^menu:generate$"),
        ],
        states={
            GEN_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, gen_prompt)],
            GEN_NODE: [CallbackQueryHandler(gen_node, pattern=r"^node:(nb|gpt)$")],
            GEN_MODEL: [CallbackQueryHandler(gen_model, pattern=r"^model:")],
            GEN_RATIO: [CallbackQueryHandler(gen_ratio, pattern=r"^ratio:")],
            GEN_RES: [CallbackQueryHandler(gen_res, pattern=r"^res:")],
            GEN_GPT_QUALITY: [CallbackQueryHandler(gen_gpt_quality, pattern=r"^gptq:")],
            GEN_GPT_ASPECT: [CallbackQueryHandler(gen_gpt_aspect, pattern=r"^gpta:")],
            GEN_GPT_BG: [CallbackQueryHandler(gen_gpt_bg, pattern=r"^gptb:")],
        },
        fallbacks=cancel_handlers,
        name="generate",
        persistent=False,
    )

    conv_img2img = ConversationHandler(
        entry_points=[
            CommandHandler("img2img", i2i_start),
            CallbackQueryHandler(i2i_start, pattern=r"^menu:img2img$"),
            CallbackQueryHandler(as_i2i_cb, pattern=r"^asi2i:"),
        ],
        states={
            I2I_COLLECT: [
                MessageHandler(img_filter, i2i_photo),
                CommandHandler("done", i2i_done),
            ],
            I2I_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, i2i_prompt)],
            I2I_NODE: [CallbackQueryHandler(i2i_node, pattern=r"^node:(nb|gpt)$")],
            I2I_MODEL: [CallbackQueryHandler(i2i_model, pattern=r"^model:")],
            I2I_RATIO: [CallbackQueryHandler(i2i_ratio, pattern=r"^ratio:")],
            I2I_RES: [CallbackQueryHandler(i2i_res, pattern=r"^res:")],
            I2I_GPT_QUALITY: [CallbackQueryHandler(i2i_gpt_quality, pattern=r"^gptq:")],
            I2I_GPT_ASPECT: [CallbackQueryHandler(i2i_gpt_aspect, pattern=r"^gpta:")],
            I2I_GPT_BG: [CallbackQueryHandler(i2i_gpt_bg, pattern=r"^gptb:")],
        },
        fallbacks=cancel_handlers,
        name="img2img",
        persistent=False,
    )

    conv_prep = ConversationHandler(
        entry_points=[
            CommandHandler("prep_speaker", sp_start),
            CallbackQueryHandler(sp_start, pattern=r"^menu:prep_speaker$"),
        ],
        states={
            SP_PHOTO: [MessageHandler(img_filter, sp_photo)],
            SP_GENDER: [CallbackQueryHandler(sp_gender, pattern=r"^gender:(man|woman)$")],
            SP_RATIO: [CallbackQueryHandler(sp_ratio, pattern=r"^ratio:")],
            SP_RES: [CallbackQueryHandler(sp_res, pattern=r"^res:")],
        },
        fallbacks=cancel_handlers,
        name="prep_speaker",
        persistent=False,
    )

    return [conv_generate, conv_img2img, conv_prep]


def build_extra_handlers() -> list:
    """Стандалон-хендлеры, регистрируются после ConversationHandler:
      - /menu — открыть главное меню
      - menu_router — обрабатывает menu:help (menu:generate/img2img/prep_speaker уходят в conv-ы)
      - regen_cb / edit_prompt_cb — на инлайн-кнопках готового результата
      - pending_edit_listener — глобальный MessageHandler в group=1, чтобы НЕ перехватывать
        текст активной conversation (она в group=0 заберёт первой).
    """
    return [
        ("group0", CommandHandler("menu", cmd_menu)),
        ("group0", CallbackQueryHandler(menu_router, pattern=r"^menu:help$")),
        ("group0", CallbackQueryHandler(regen_cb, pattern=r"^regen:")),
        ("group0", CallbackQueryHandler(edit_prompt_cb, pattern=r"^editp:")),
        # Глобальный listener в group=1 — после conversations.
        ("group1", MessageHandler(filters.TEXT & ~filters.COMMAND, pending_edit_listener)),
    ]
