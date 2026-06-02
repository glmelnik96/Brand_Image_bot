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
    InputMediaPhoto,
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
from bot.feedback import (
    build_feedback_handlers,
    on_generation_success,
    rating_kb,
)
from bot.state import USER_QUEUE_LIMIT, TaskRecipe, get_state
from bot.status_reporter import StatusReporter
from client.api import _SSL_CTX, PhygitalClient
from client.models import GenerationJob
from workflows.brand_img2img import run_brand_img2img
from workflows.brand_text2img import run_brand_text2img
from workflows.image_gen import ImageGenWorkflow
from workflows.image_to_image import ImageToImageWorkflow
from workflows.midjourney import MJUpscaleWorkflow, MJVariationWorkflow
from workflows.photoroom import PhotoroomBgRemoveWorkflow
from workflows.who_is_who import run_who_is_who
from workflows.speaker_prep import (
    BG_COLORS,
    DEFAULT_REFERENCE,
    bg_swap_prompt,
    build_speaker_prep_workflow,
    speaker_prompt,
)

# ── state IDs ──────────────────────────────────────────────────────────────
# /generate: prompt → ratio → resolution (модель и нода зашиты: Nano Banana v3.1).
GEN_PROMPT, GEN_RATIO, GEN_RES = range(1, 4)
# /img2img: collect → prompt → ratio → resolution.
I2I_COLLECT, I2I_PROMPT, I2I_RATIO, I2I_RES = range(10, 14)
# /prep_speaker
SP_PHOTO, SP_GENDER, SP_RATIO, SP_RES = range(20, 24)
# /brand_generate: prompt → ratio → resolution.
# UX как /generate, между текстом и Nano Banana вставлен Gemini Text (CloudRu Enhancer).
BT2I_PROMPT, BT2I_RATIO, BT2I_RES = range(30, 33)
# /brand_img2img: collect (1-4) → /done → ratio → resolution. Пользователь НЕ пишет промпт —
# Gemini Text запускается с фикс. text_prompt="Read the System Prompt" + system-prompt-документом.
BI2I_COLLECT, BI2I_RATIO, BI2I_RES = range(40, 43)
# Who is who: prompt → MJ Imagine (через Gemini Flash). Видим только UID из WIW_UIDS.
WIW_PROMPT_STATE, = range(50, 51)

# Who is who — внутренний/exper-ный сценарий, видим в главном меню только этим UID.
WIW_UIDS: frozenset[int] = frozenset({820187903, 438074662})

TG_PHOTO_LIMIT_BYTES = 10 * 1024 * 1024

# Ориентир по времени генерации.
# Nano Banana — медиана ~37s из реальных логов (6 задач 13 мая: 27–87s).
ETA_NANO_BANANA_SEC = 45
# Brand-сценарии = два последовательных task'а (Gemini Text ~25–40s + Nano Banana ~30–60s).
ETA_BRAND_SEC = 90
# Photoroom (удаление фона) — быстрая нода, по UI ~5–15s.
ETA_PHOTOROOM_SEC = 20
# Who is who = Gemini Flash (короткий ~10-25s) + MJ Imagine (~60-120s).
ETA_WIW_SEC = 120
# MJ Upscale / Variation — без Gemini, отдельные ноды Midjourney.
ETA_MJ_UPSCALE_SEC = 40
ETA_MJ_VARIATION_SEC = 70

# Фиксированный вариант модели Nano Banana — Phygital поддерживает v2/v2.5/v3/v3.1,
# боту оставили только v3.1 (Pro), чтобы убрать лишний шаг пикера.
NANO_BANANA_MODEL = "v3_1"


def _fmt_eta(seconds: int) -> str:
    """'~3.5 мин' / '~8 мин'."""
    m = seconds / 60.0
    if m < 1:
        return f"~{int(seconds)} сек"
    if m < 10:
        return f"~{m:.1f} мин"
    return f"~{int(m)} мин"


def _escape_prompt(prompt: str) -> str:
    """Чистит prompt для записи в логи: переводы строк → \\n, чтобы каждая запись была одной строкой.
    Используется в `prompt=...` маркерах, которые потом парсит `tools/digest.py`."""
    return prompt.replace("\r\n", "\\n").replace("\n", "\\n")

# ── параметры нод (взяты из GET /api/v2/nodes/, проверено 2026-05-17) ──────
# Gemini Image API (Nano Banana), workflow id=94. 15 ratios — полный enum Phygital+.
GEMINI_RATIOS: list[tuple[str, str]] = [
    ("default", "auto"),
    ("r_1_1", "1:1"),
    ("r_3_4", "3:4"),
    ("r_4_3", "4:3"),
    ("r_2_3", "2:3"),
    ("r_3_2", "3:2"),
    ("r_4_5", "4:5"),
    ("r_5_4", "5:4"),
    ("r_9_16", "9:16"),
    ("r_16_9", "16:9"),
    ("r_21_9", "21:9"),
    ("r_1_4", "1:4"),
    ("r_4_1", "4:1"),
    ("r_1_8", "1:8"),
    ("r_8_1", "8:1"),
]
GEMINI_RESOLUTIONS: list[tuple[str, str]] = [
    ("default", "auto"),
    ("k1", "1K"),
    ("k2", "2K"),
    ("k4", "4K"),
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
        rows.append([InlineKeyboardButton("Отмена", callback_data="cancel:picker")])
    return InlineKeyboardMarkup(rows)


def _action_keyboard(task_uid: str, *, workflow: str = "nb_t2i") -> InlineKeyboardMarkup:
    """Клавиатура под готовый результат — кнопки повторного использования.

    Раскладка зависит от сценария:
      - nb_t2i (обычная T2I): [Повторить] [Изменить текст]
                              [Изменить изображение] [Добавить Brand patterns]
      - brand_t2i (Photo/Render/Isometry): [Повторить] [Изменить изображение]
      - speaker / speaker_bg: [Повторить] + строка кнопок «Сменить фон: <цвет>»
      - все остальные (nb_i2i, brand_i2i): [Повторить]

    «Изменить текст» оставлена ТОЛЬКО на обычной T2I — единственный сценарий,
    где у пользователя есть собственный текстовый промпт, который имеет смысл
    править между запусками. Brand-сценарии гонят промпт через Gemini Text,
    img2img/speaker — без пользовательского текста.

    Колбэки `asi2i:` («Изменить изображение») и `asbi2i:` («Добавить Brand
    patterns») остались прежними — лишь переименованы лейблы.
    spkrbg:<task_uid>:<HEX> — смена фона на однотонный (см. speaker_bg_cb).
    """
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("Повторить", callback_data=f"regen:{task_uid}")]
    ]
    if workflow == "nb_t2i":
        rows.append([InlineKeyboardButton("Изменить текст", callback_data=f"editp:{task_uid}")])
        rows.append([
            InlineKeyboardButton("Изменить изображение", callback_data=f"asi2i:{task_uid}"),
            InlineKeyboardButton("Добавить Brand patterns", callback_data=f"asbi2i:{task_uid}"),
        ])
    elif workflow == "brand_t2i":
        rows.append([
            InlineKeyboardButton("Изменить изображение", callback_data=f"asi2i:{task_uid}"),
        ])
    elif workflow in ("speaker", "speaker_bg"):
        # 5 brand-цветов под результатом портрета: разбиваем 3+2 чтобы метки помещались.
        color_btns = [
            InlineKeyboardButton(name, callback_data=f"spkrbg:{task_uid}:{hex_code}")
            for hex_code, name in BG_COLORS
        ]
        rows.append(color_btns[:3])
        if color_btns[3:]:
            rows.append(color_btns[3:])
        rows.append([
            InlineKeyboardButton("Удалить фон", callback_data=f"spkrrmbg:{task_uid}")
        ])
    elif workflow == "speaker_nobg":
        # После удаления фона — оставляем только повторное удаление (рекурсивно,
        # вдруг результат не понравился — можно прогнать ещё раз тот же исходник).
        rows.append([
            InlineKeyboardButton("Удалить фон ещё раз", callback_data=f"spkrrmbg:{task_uid}")
        ])
    elif workflow == "mj_upscale":
        # Upscale — финальная картинка, дальше U/V не нужны.
        pass
    # mj_variation / wiw_imagine рендерятся через _send_mj_grid_with_actions
    # (media_group + follow-up с U/V), сюда не попадают.
    # nb_i2i / brand_i2i — только «Повторить»
    # 👍/👎 — последняя строка для всех сценариев.
    rows.append(rating_kb(task_uid, workflow))
    return InlineKeyboardMarkup(rows)


def _mj_grid_actions_kb(task_uid: str, workflow: str = "wiw_imagine") -> InlineKeyboardMarkup:
    """Кнопки под grid'ом из 4 MJ-картинок: U1-U4 (Upscale), V1-V4 (Variation),
    плюс «Повторить» и оценка 👍/👎. Колбэки:
      mjact:U:<task_uid>:<idx>  — Upscale картинки idx ∈ 1..4
      mjact:V:<task_uid>:<idx>  — Variation картинки idx ∈ 1..4
      regen:<task_uid>          — повторить исходную задачу с новой сидой
    """
    u_row = [
        InlineKeyboardButton(f"U{i}", callback_data=f"mjact:U:{task_uid}:{i}")
        for i in range(1, 5)
    ]
    v_row = [
        InlineKeyboardButton(f"V{i}", callback_data=f"mjact:V:{task_uid}:{i}")
        for i in range(1, 5)
    ]
    return InlineKeyboardMarkup([
        u_row,
        v_row,
        [InlineKeyboardButton("Повторить", callback_data=f"regen:{task_uid}")],
        rating_kb(task_uid, workflow),
    ])


# ── главное меню и подменю ─────────────────────────────────────────────────
# Иерархия:
#   root → make → make_brand → {photo, render, isometric}
#                └ generate (обычное text→image)
#        → edit → {img2img, brand_img2img}
#        → prep_speaker
#        → help
def _menu_root_kb_for(uid: int | None = None) -> InlineKeyboardMarkup:
    """Главное меню. Для UID из WIW_UIDS добавляется кнопка «Who is who»
    (экспериментальный MJ-сценарий, остальным не показываем)."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("Создай изображение", callback_data="menu:make")],
        [InlineKeyboardButton("Изменить изображение", callback_data="menu:edit")],
        [InlineKeyboardButton("Фотография спикера", callback_data="menu:prep_speaker")],
    ]
    if uid is not None and uid in WIW_UIDS:
        rows.append([InlineKeyboardButton("Who is who", callback_data="menu:wiw")])
    rows.extend([
        [InlineKeyboardButton("Обратная связь", callback_data="menu:feedback")],
        [InlineKeyboardButton("Помощь", callback_data="menu:help")],
    ])
    return InlineKeyboardMarkup(rows)


def _menu_root_kb() -> InlineKeyboardMarkup:
    """Backward-compat alias — без UID-инфы, без Who-is-who-кнопки."""
    return _menu_root_kb_for(None)


def _menu_make_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Бренд изображения", callback_data="menu:make_brand")],
        [InlineKeyboardButton("Обычное изображение", callback_data="menu:generate")],
        [InlineKeyboardButton("Назад", callback_data="menu:root")],
    ])


def _menu_make_brand_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Photo", callback_data="menu:brand_photo")],
        [InlineKeyboardButton("Render", callback_data="menu:brand_render")],
        [InlineKeyboardButton("2d Isometry", callback_data="menu:brand_isometric")],
        [InlineKeyboardButton("Назад", callback_data="menu:make")],
    ])


def _menu_edit_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Изменить изображение", callback_data="menu:img2img")],
        [InlineKeyboardButton("Добавить Brand patterns", callback_data="menu:brand_img2img")],
        [InlineKeyboardButton("Назад", callback_data="menu:root")],
    ])


# Алиас для совместимости с импортом из bot/main.py (cmd_start).
MENU_KEYBOARD = _menu_root_kb()


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
            f"Очередь заполнена: {USER_QUEUE_LIMIT} задач "
            f"(в работе {inflight}, ожидают {queued}). "
            "Дождись, пока что-то закончится."
        )
        target = _first_message(update)
        await target.reply_text(msg)
        return False
    return True


# Маппинг workflow → имя первого этапа для StatusReporter.
# Brand-сценарии стартуют с Gemini Text, остальные — сразу с Nano Banana.
_FIRST_STEP_BY_WORKFLOW: dict[str, str] = {
    "nb_t2i": "Nano Banana",
    "nb_i2i": "Nano Banana",
    "brand_t2i": "Gemini Text",
    "brand_i2i": "Gemini Text",
    "speaker": "Nano Banana",
    "speaker_bg": "Nano Banana",
    "speaker_nobg": "Photoroom",
    "wiw_imagine": "Gemini Flash",
    "mj_upscale": "MJ Upscale",
    "mj_variation": "MJ Variation",
}

# Сценарии, чьи result_urls приходят grid-ом из 4 картинок и должны рендериться
# как media_group + follow-up с кнопками U1-U4/V1-V4.
_MJ_GRID_WORKFLOWS: frozenset[str] = frozenset({"wiw_imagine", "mj_variation"})


def _initial_step_for(recipe_template: TaskRecipe | None) -> str:
    if recipe_template is None:
        return "Генерация"
    return _FIRST_STEP_BY_WORKFLOW.get(recipe_template.workflow, "Генерация")


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

    inflight, queued = state.user_load(uid)
    total_after = inflight + queued + 1
    if total_after > USER_QUEUE_LIMIT:
        log.warning(f"rejected: user queue full ({inflight} inflight + {queued} queued)")
        await chat.send_message(
            f"Очередь заполнена ({USER_QUEUE_LIMIT} задач). "
            "Дождись, пока что-то закончится."
        )
        return

    queue_pos = inflight + queued + 1  # 1-based «эта задача — N-я твоя в полёте»
    # Начальный текст. StatusReporter перерисует его, как только дошли до _execute_task.
    eta_hint = f" • ожидаемое время {_fmt_eta(eta_sec)}" if eta_sec else ""
    status: Message = await chat.send_message(
        f"{label} — принято\nочередь №{queue_pos}{eta_hint}"
    )
    reporter = StatusReporter(status=status, label=label, eta_sec=eta_sec)
    # Не дожидаемся отправки initial-сообщения (оно уже ушло), но синхронизуем внутреннее
    # состояние reporter'а — на случай если queue_pos > 1 и юзер успеет увидеть «в очереди».
    await reporter.queued(queue_pos=queue_pos)

    queued_at = time.monotonic()
    task_uid = uuid.uuid4().hex[:8]

    async def task_lane() -> None:
        await _execute_task(
            ctx=ctx, uid=uid, uname=uname, chat=chat,
            runner=runner, label=label, eta_sec=eta_sec,
            reporter=reporter, queued_at=queued_at, task_uid=task_uid,
            cleanup_paths=cleanup_paths or [],
            recipe_template=recipe_template,
        )

    accepted = await state.submit_task(uid, task_lane)
    if not accepted:
        # Между user_load и submit_task проскочила другая задача — отбиваемся.
        log.warning("rejected at submit: queue full race")
        await reporter.error("Очередь заполнилась прямо сейчас. Попробуй ещё раз.")
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
    reporter: StatusReporter,
    queued_at: float,
    task_uid: str,
    cleanup_paths: list[Path],
    recipe_template: TaskRecipe | None,
) -> None:
    """Реально запускает задачу: ждёт глобальный семафор, дёргает runner, шлёт результат.
    На успехе сохраняет recipe + первую result-картинку в regen_cache для пост-задачных кнопок."""
    state = get_state()
    log = logger.bind(uid=uid, uname=uname, label=label, task=task_uid)

    # Сразу показываем «жду слот» — ticker побежит даже до global_sem.
    await reporter.waiting_slot()

    try:
        async with state.global_sem:
            queue_wait = time.monotonic() - queued_at
            log.info(f"started (queue_wait={queue_wait:.1f}s)")
            first_step = _initial_step_for(recipe_template)
            await reporter.start(first_step)
            run_started = time.monotonic()

            job = await runner(progress_cb=reporter.step, pct_cb=reporter.progress)
            gen_dur = time.monotonic() - run_started

        log = log.bind(job_id=job.job_id)
        log.info(
            f"job result status={job.status} urls={len(job.result_urls)} dur={gen_dur:.1f}s"
        )

        if job.status == "completed" and job.result_urls:
            await reporter.done(job_id=str(job.job_id))
            workflow_for_kb = recipe_template.workflow if recipe_template else "nb_t2i"
            send_as_document = workflow_for_kb == "speaker_nobg"
            first_result_local: Path | None = None
            all_results_local: list[Path] = []

            if workflow_for_kb in _MJ_GRID_WORKFLOWS:
                # MJ grid (wiw_imagine / mj_variation): media_group из 4 картинок + follow-up
                # с U1-U4/V1-V4 кнопками. Telegram не позволяет inline-кнопки на media_group,
                # поэтому follow-up — отдельным сообщением.
                all_results_local = await _send_mj_grid_with_actions(
                    chat=chat, log=log, urls=job.result_urls, uid=uid,
                    task_uid=task_uid, workflow=workflow_for_kb,
                )
                if all_results_local:
                    first_result_local = all_results_local[0]
            else:
                # Скачиваем + отправляем каждую картинку; ловим путь к первой для regen-cache.
                for i, url in enumerate(job.result_urls, 1):
                    local = await _send_result_image(
                        chat=chat, log=log, url=url, uid=uid, task_uid=task_uid,
                        idx=i, total=len(job.result_urls),
                        with_action_kb=(i == 1 and recipe_template is not None),
                        action_task_uid=task_uid,
                        action_workflow=workflow_for_kb,
                        as_document=send_as_document,
                    )
                    if i == 1 and local is not None:
                        first_result_local = local
                    if local is not None:
                        all_results_local.append(local)

            # mj_task_id — нужен для U/V-кнопок над grid'ом (см. mjact_cb).
            # Для wiw_imagine он лежит в job.raw["mj_task_id"], для mj_variation —
            # task_id её самой (она тоже отдаёт grid).
            mj_task_id: int | None = None
            if workflow_for_kb in _MJ_GRID_WORKFLOWS:
                raw_mj = (job.raw or {}).get("mj_task_id")
                if isinstance(raw_mj, int):
                    mj_task_id = raw_mj
                else:
                    try:
                        mj_task_id = int(job.job_id)
                    except (TypeError, ValueError):
                        mj_task_id = None

            # Сохраняем recipe (если был template) — после отправки картинки.
            if recipe_template is not None:
                try:
                    _persist_recipe(
                        state=state, template=recipe_template, task_uid=task_uid,
                        cleanup_paths=cleanup_paths, first_result=first_result_local,
                        all_results=all_results_local, mj_task_id=mj_task_id, log=log,
                    )
                except Exception as e:
                    log.opt(exception=e).warning(f"recipe save failed: {e!r}")

            # Хук обратной связи: считаем успешную генерацию + (по триггеру) шлём баннер опроса.
            # Не блокируем основной поток исключениями из feedback-подсистемы.
            try:
                async def _send_followup(text: str, reply_markup=None):
                    await chat.send_message(text, reply_markup=reply_markup)
                await on_generation_success(uid, _send_followup)
            except Exception as e:
                log.opt(exception=e).warning(f"feedback hook failed: {e!r}")
        elif job.status == "completed":
            log.error("completed but result_urls is empty")
            await reporter.error("задача завершилась, но Phygital не вернул ссылки на файлы")
        else:
            log.warning(f"job not completed: {job.status} err={job.error!r}")
            await reporter.error(job.error or job.status)
    except Exception as e:
        log.opt(exception=e).error(f"task crashed: {type(e).__name__}: {e}")
        await reporter.crashed(f"{type(e).__name__}: {e}")
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
    all_results: list[Path] | None = None,
    mj_task_id: int | None = None,
    log,
) -> None:
    """Завершает заготовку recipe и кладёт её в state.recipes.
    Копирует init-файлы (cleanup_paths) в regen_dir, чтобы они пережили cleanup.
    `all_results` — список всех скачанных картинок (для MJ grid'ов из 4 шт.).
    `mj_task_id` — int task_id MJ Imagine/Variation, нужен для U/V кнопок."""
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
        result_paths=list(all_results or ([first_result] if first_result else [])),
        mj_task_id=mj_task_id,
    )
    state.save_recipe(recipe)
    log.info(
        f"recipe saved: workflow={recipe.workflow} inits={len(cached_inits)} "
        f"results={len(recipe.result_paths)} mj_task_id={mj_task_id} "
        f"has_result={first_result is not None}"
    )


async def _send_mj_grid_with_actions(
    *, chat: Chat, log, urls: list[str], uid: int, task_uid: str,
    workflow: str,
) -> list[Path]:
    """Скачивает все картинки grid'а в regen_dir, шлёт media_group, затем follow-up
    с U1-U4/V1-V4 кнопками. Возвращает список локальных путей (в порядке urls).

    Telegram media_group не поддерживает inline-кнопки, поэтому follow-up — отдельным
    text-сообщением с _mj_grid_actions_kb. Сообщение не несёт картинки — только
    короткий текст «Готово, выбери U/V» + клавиатура.
    """
    state = get_state()
    regen_dir = state.regen_dir(uid, task_uid)
    locals_: list[Path] = []
    for i, url in enumerate(urls, 1):
        local = regen_dir / f"result_{i}.png"
        try:
            async with httpx.AsyncClient(timeout=120, follow_redirects=True, verify=_SSL_CTX) as cli:
                r = await cli.get(url)
                r.raise_for_status()
                local.write_bytes(r.content)
            locals_.append(local)
            log.info(
                f"mj-grid: downloaded {i}/{len(urls)} → {local.name} "
                f"({local.stat().st_size / 1024 / 1024:.2f}MB)"
            )
        except Exception as e:
            log.warning(f"mj-grid: download {i}/{len(urls)} failed: {type(e).__name__}: {e}")

    if not locals_:
        # Ничего не скачали — пробуем сырыми URL'ами через media_group.
        try:
            media = [InputMediaPhoto(media=u) for u in urls[:10]]
            await chat.send_media_group(media=media)
            log.info("mj-grid: sent media_group via raw urls (no local copies)")
        except Exception as e:
            log.warning(f"mj-grid: raw-url media_group failed: {type(e).__name__}: {e}")
            for u in urls:
                try:
                    await chat.send_message(f"Картинка: {u}")
                except Exception:
                    pass
    else:
        try:
            opened = []
            files = []
            for p in locals_:
                fh = p.open("rb")
                files.append(fh)
                opened.append(InputMediaPhoto(media=InputFile(fh, filename=p.name)))
            try:
                await chat.send_media_group(media=opened)
                log.info(f"mj-grid: sent media_group of {len(opened)} photos")
            finally:
                for fh in files:
                    try:
                        fh.close()
                    except Exception:
                        pass
        except Exception as e:
            log.warning(f"mj-grid: media_group send failed: {type(e).__name__}: {e}")
            # Fallback — по одной картинке.
            for i, p in enumerate(locals_, 1):
                try:
                    with p.open("rb") as fh:
                        await chat.send_photo(photo=InputFile(fh, filename=p.name))
                except Exception as ee:
                    log.warning(f"mj-grid: fallback photo {i} failed: {ee!r}")

    # Follow-up с U/V кнопками.
    try:
        await chat.send_message(
            "Готово. Выбери действие над одной из четырёх:\n"
            "U1-U4 — улучшить (Upscale), V1-V4 — сделать вариации.",
            reply_markup=_mj_grid_actions_kb(task_uid, workflow=workflow),
        )
    except Exception as e:
        log.warning(f"mj-grid: follow-up keyboard failed: {type(e).__name__}: {e}")
    return locals_


async def _send_result_image(
    *, chat: Chat, log, url: str, uid: int, task_uid: str, idx: int, total: int,
    with_action_kb: bool = False, action_task_uid: str | None = None,
    action_workflow: str = "nb_t2i",
    as_document: bool = False,
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
    kb = (
        _action_keyboard(action_task_uid, workflow=action_workflow)
        if (with_action_kb and action_task_uid)
        else None
    )

    # Если нужны кнопки + локальный путь (для asi2i) — пропускаем url-стратегию,
    # сразу качаем и шлём upload. Иначе оптимизируем под скорость и пробуем url первым.
    # Если as_document=True (например, PNG с прозрачностью после Photoroom) — тоже всегда
    # качаем и шлём через send_document, иначе Telegram пожмёт в JPEG и убьёт alpha.
    if not with_action_kb and not as_document:
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
            if as_document or size > TG_PHOTO_LIMIT_BYTES:
                await chat.send_document(document=inp, reply_markup=kb)
                log.info(
                    f"sent {idx}/{total} as document "
                    f"({'forced' if as_document else 'size>10MB'})"
                )
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
                f"Не смог отправить картинку {idx}/{total}. Ссылка:\n{url}",
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
            await update.callback_query.edit_message_text("Отменено.")
        except Exception:
            pass
    elif update.message:
        await update.message.reply_text(
            "Отменено. Уже запущенные задачи в Phygital продолжают выполняться."
        )
    return ConversationHandler.END


# ── GPT Image: первая ступень пикера после выбора ноды ────────────────────
# ── /generate ──────────────────────────────────────────────────────────────
@whitelist_only
async def gen_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _check_user_capacity(update):
        return ConversationHandler.END
    log = _ulog(update, "generate")
    # Вход только через menu-callback — подтверждаем нажатие.
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass
    target = _first_message(update)
    log.info("entered prompt-collection state")
    await target.reply_text(
        "Опиши текстом, что нужно нарисовать. /cancel — выйти."
    )
    return GEN_PROMPT


@whitelist_only
async def gen_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    log = _ulog(update, "/generate")
    prompt = (update.message.text or "").strip()
    if not prompt:
        log.warning("empty prompt, asking again")
        await update.message.reply_text(
            "Пустое описание. Пришли текст ещё раз или /cancel."
        )
        return GEN_PROMPT
    ctx.user_data["prompt"] = prompt
    log.info(f"prompt received: chars={len(prompt)} prompt={_escape_prompt(prompt)!r}; asking for ratio")
    await update.message.reply_text(
        "Выбери соотношение сторон. Кнопка «auto» — модель подберёт сама.",
        reply_markup=_kb("ratio", GEMINI_RATIOS, cols=4),
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
        "Выбери разрешение. Кнопка «auto» — модель подберёт сама.",
        reply_markup=_kb("res", GEMINI_RESOLUTIONS, cols=4),
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
    model = NANO_BANANA_MODEL
    ratio = ctx.user_data.get("ratio", "default")
    resolution = ctx.user_data.get("resolution", "default")
    state = get_state()
    u = update.effective_user
    chat = update.effective_chat

    async def runner(progress_cb=None, pct_cb=None) -> GenerationJob:
        async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
            wf = ImageGenWorkflow(
                client, model_name=model, ratio=ratio, resolution=resolution
            )
            wf.on_progress = pct_cb
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
        "Пришли 1–4 исходные картинки (фото или файлом). "
        "Когда закончишь — отправь /done. /cancel — отменить."
    )
    return I2I_COLLECT


@whitelist_only
async def i2i_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    log = _ulog(update, "/img2img")
    path = await _download_image(update, ctx, log)
    if not path:
        log.warning("non-image payload in I2I_COLLECT")
        await update.message.reply_text(
            "Это не картинка. Пришли фото или файл-изображение."
        )
        return I2I_COLLECT
    ctx.user_data["init_paths"].append(path)
    n = len(ctx.user_data["init_paths"])
    log.info(f"collected init image #{n}: {path.name}")
    await update.message.reply_text(
        f"Принято: {n} из 4. Можешь прислать ещё или отправь /done."
    )
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
    await update.message.reply_text(
        "Теперь опиши, как изменить эти картинки. /cancel — выйти."
    )
    return I2I_PROMPT


@whitelist_only
async def i2i_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    log = _ulog(update, "/img2img")
    prompt = (update.message.text or "").strip()
    if not prompt:
        log.warning("empty prompt for img2img")
        await update.message.reply_text(
            "Пустое описание. Пришли текст ещё раз или /cancel."
        )
        return I2I_PROMPT
    ctx.user_data["prompt"] = prompt
    log.info(f"prompt received: chars={len(prompt)} prompt={_escape_prompt(prompt)!r}; asking for ratio")
    await update.message.reply_text(
        "Выбери соотношение сторон. Кнопка «auto» — модель подберёт сама.",
        reply_markup=_kb("ratio", GEMINI_RATIOS, cols=4),
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
        "Выбери разрешение. Кнопка «auto» — модель подберёт сама.",
        reply_markup=_kb("res", GEMINI_RESOLUTIONS, cols=4),
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
    model = NANO_BANANA_MODEL
    ratio = ctx.user_data.get("ratio", "r_3_4")
    state = get_state()
    u = update.effective_user
    chat = update.effective_chat

    async def runner(progress_cb=None, pct_cb=None) -> GenerationJob:
        async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
            wf = ImageToImageWorkflow(
                client, model_name=model, ratio=ratio, resolution=resolution
            )
            wf.on_progress = pct_cb
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
        "Пришли фото спикера — одним фото или файлом-изображением.\n\n"
        "Чтобы результат получился чистым, лучше заранее обрезать кадр так, "
        "чтобы в нём остался только сам человек: без фона мероприятия, "
        "посторонних людей и подписей.\n\n"
        "Если итоговый портрет получится неудачно — просто запусти сценарий ещё раз: "
        "Nano Banana каждый раз генерирует чуть иной вариант.\n\n"
        "/cancel — отменить."
    )
    return SP_PHOTO


@whitelist_only
async def sp_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    log = _ulog(update, "/prep_speaker")
    path = await _download_image(update, ctx, log)
    if not path:
        log.warning("non-image payload in SP_PHOTO")
        await update.message.reply_text("Это не картинка. Пришли фото.")
        return SP_PHOTO
    ctx.user_data["speaker_photo"] = path
    log.info(f"speaker photo saved: {path.name}")
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Мужчина", callback_data="gender:man"),
            InlineKeyboardButton("Женщина", callback_data="gender:woman"),
        ],
        [InlineKeyboardButton("Отмена", callback_data="cancel:picker")],
    ])
    await update.message.reply_text(
        "Кто на фото? Это нужно, чтобы подобрать подходящий промпт.",
        reply_markup=kb,
    )
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
        "Выбери соотношение сторон. Для портрета спикера обычно лучше 3:4.",
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
        "Выбери разрешение. Для портрета спикера обычно достаточно 2K.",
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

    async def runner(progress_cb=None, pct_cb=None) -> GenerationJob:
        async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
            wf = build_speaker_prep_workflow(client)
            # Перебиваем дефолтные параметры выбором пользователя.
            wf.ratio = ratio
            wf.resolution = resolution
            wf.on_progress = pct_cb
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


# ── brand text→image (Photo / Render / 2d Isometry) ───────────────────────
# Brand-консистентный text→image: user text → Gemini Text (variant system-prompt) → Nano Banana.
# Вариант определяется callback_data входной menu-кнопки:
#   menu:brand_photo     → variant="photo"
#   menu:brand_render    → variant="render"
#   menu:brand_isometric → variant="isometric"
_BRAND_VARIANT_BY_CALLBACK = {
    "menu:brand_photo": "photo",
    "menu:brand_render": "render",
    "menu:brand_isometric": "isometric",
}
_BRAND_VARIANT_LABEL = {
    "photo": "Photo",
    "render": "Render",
    "isometric": "2d Isometry",
}


@whitelist_only
async def bt2i_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _check_user_capacity(update):
        return ConversationHandler.END
    log = _ulog(update, "brand_generate")
    variant = "photo"  # дефолт на случай если попали сюда не через callback
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass
        variant = _BRAND_VARIANT_BY_CALLBACK.get(
            update.callback_query.data, "photo"
        )
    ctx.user_data["variant"] = variant
    log.info(f"entered prompt-collection state (variant={variant})")
    target = _first_message(update)
    await target.reply_text(
        f"Бренд-вариант: <b>{_BRAND_VARIANT_LABEL[variant]}</b>.\n"
        "Опиши идею картинки в свободной форме. Gemini подготовит брендовый "
        "промпт под Cloud.ru, потом Nano Banana нарисует. /cancel — выйти.",
        parse_mode="HTML",
    )
    return BT2I_PROMPT


@whitelist_only
async def bt2i_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    log = _ulog(update, "/brand_generate")
    prompt = (update.message.text or "").strip()
    if not prompt:
        log.warning("empty prompt, asking again")
        await update.message.reply_text(
            "Пустое описание. Пришли текст ещё раз или /cancel."
        )
        return BT2I_PROMPT
    ctx.user_data["prompt"] = prompt
    log.info(f"prompt received: chars={len(prompt)} prompt={_escape_prompt(prompt)!r}; asking for ratio")
    await update.message.reply_text(
        "Выбери соотношение сторон. Кнопка «auto» — модель подберёт сама.",
        reply_markup=_kb("ratio", GEMINI_RATIOS, cols=4),
    )
    return BT2I_RATIO


@whitelist_only
async def bt2i_ratio(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    ratio = q.data.split(":", 1)[1]
    ctx.user_data["ratio"] = ratio
    label = dict(GEMINI_RATIOS).get(ratio, ratio)
    _ulog(update, "/brand_generate").info(f"ratio: {ratio}")
    await _edit_picked(q, "Соотношение", label)
    await q.message.chat.send_message(
        "Выбери разрешение. Кнопка «auto» — модель подберёт сама.",
        reply_markup=_kb("res", GEMINI_RESOLUTIONS, cols=4),
    )
    return BT2I_RES


@whitelist_only
async def bt2i_res(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    resolution = q.data.split(":", 1)[1]
    ctx.user_data["resolution"] = resolution
    label = dict(GEMINI_RESOLUTIONS).get(resolution, resolution)
    _ulog(update, "/brand_generate").info(f"resolution: {resolution}; launching")
    await _edit_picked(q, "Разрешение", label)
    await _bt2i_run(update, ctx)
    return ConversationHandler.END


async def _bt2i_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    prompt: str = ctx.user_data["prompt"]
    variant: str = ctx.user_data.get("variant", "photo")
    model = NANO_BANANA_MODEL
    ratio = ctx.user_data.get("ratio", "default")
    resolution = ctx.user_data.get("resolution", "default")
    state = get_state()
    u = update.effective_user
    chat = update.effective_chat

    async def runner(progress_cb=None, pct_cb=None) -> GenerationJob:
        async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
            return await run_brand_text2img(
                client, prompt=prompt, variant=variant, model_name=model,
                ratio=ratio, resolution=resolution,
                progress_cb=progress_cb, pct_cb=pct_cb,
            )

    label = f"brand_t2i:{variant}/{model}/{ratio}/{resolution}"
    template = TaskRecipe(
        task_uid="", user_id=u.id,
        label=label,
        workflow="brand_t2i", prompt=prompt,
        params={
            "model": model, "ratio": ratio, "resolution": resolution,
            "variant": variant,
        },
    )
    await _enqueue_task(
        ctx=ctx,
        uid=u.id,
        uname=u.username or u.full_name or "?",
        chat=chat,
        runner=runner,
        label=label,
        eta_sec=ETA_BRAND_SEC,
        recipe_template=template,
    )


# ── /brand_img2img ─────────────────────────────────────────────────────────
# Brand-консистентный image→image: user image(s) → Gemini Text (фикс. prompt + CloudRu Img2Img) → Nano Banana.
@whitelist_only
async def bi2i_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _check_user_capacity(update):
        return ConversationHandler.END
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass
    _ulog(update, "/brand_img2img").info("started")
    ctx.user_data["init_paths"] = []
    target = _first_message(update)
    await target.reply_text(
        "Пришли 1–4 исходные картинки (фото или файлом). Описание не нужно — "
        "Gemini Text сам подготовит брендовый промпт под Cloud.ru.\n"
        "Когда закончишь — отправь /done. /cancel — отменить."
    )
    return BI2I_COLLECT


@whitelist_only
async def bi2i_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    log = _ulog(update, "/brand_img2img")
    path = await _download_image(update, ctx, log)
    if not path:
        log.warning("non-image payload in BI2I_COLLECT")
        await update.message.reply_text(
            "Это не картинка. Пришли фото или файл-изображение."
        )
        return BI2I_COLLECT
    ctx.user_data["init_paths"].append(path)
    n = len(ctx.user_data["init_paths"])
    log.info(f"collected init image #{n}: {path.name}")
    await update.message.reply_text(
        f"Принято: {n} из 4. Можешь прислать ещё или отправь /done."
    )
    return BI2I_COLLECT


@whitelist_only
async def bi2i_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    log = _ulog(update, "/brand_img2img")
    n = len(ctx.user_data.get("init_paths") or [])
    if not n:
        log.warning("/done before any image")
        await update.message.reply_text("Сначала пришли хотя бы одну картинку.")
        return BI2I_COLLECT
    log.info(f"done collecting, total={n}; asking for ratio")
    await update.message.reply_text(
        "Выбери соотношение сторон. Кнопка «auto» — модель подберёт сама.",
        reply_markup=_kb("ratio", GEMINI_RATIOS, cols=4),
    )
    return BI2I_RATIO


@whitelist_only
async def bi2i_ratio(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    ratio = q.data.split(":", 1)[1]
    ctx.user_data["ratio"] = ratio
    label = dict(GEMINI_RATIOS).get(ratio, ratio)
    _ulog(update, "/brand_img2img").info(f"ratio: {ratio}")
    await _edit_picked(q, "Соотношение", label)
    await q.message.chat.send_message(
        "Выбери разрешение. Кнопка «auto» — модель подберёт сама.",
        reply_markup=_kb("res", GEMINI_RESOLUTIONS, cols=4),
    )
    return BI2I_RES


@whitelist_only
async def bi2i_res(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    resolution = q.data.split(":", 1)[1]
    ctx.user_data["resolution"] = resolution
    label = dict(GEMINI_RESOLUTIONS).get(resolution, resolution)
    log = _ulog(update, "/brand_img2img")
    log.info(f"resolution: {resolution}; launching")
    await _edit_picked(q, "Разрешение", label)

    init_paths: list[Path] = ctx.user_data["init_paths"]
    model = NANO_BANANA_MODEL
    ratio = ctx.user_data.get("ratio", "r_3_4")
    state = get_state()
    u = update.effective_user
    chat = update.effective_chat

    async def runner(progress_cb=None, pct_cb=None) -> GenerationJob:
        async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
            return await run_brand_img2img(
                client, init_paths=init_paths,
                model_name=model, ratio=ratio, resolution=resolution,
                progress_cb=progress_cb, pct_cb=pct_cb,
            )

    # prompt в recipe — пустая строка: пользовательского текста нет, фикс. "Read the System Prompt"
    # держим в композере. Кнопка «Изменить текст» в _action_keyboard для brand_i2i скрыта.
    template = TaskRecipe(
        task_uid="", user_id=u.id,
        label=f"brand_i2i/{model}/{ratio}/{resolution}",
        workflow="brand_i2i", prompt="",
        params={"model": model, "ratio": ratio, "resolution": resolution},
    )
    await _enqueue_task(
        ctx=ctx,
        uid=u.id,
        uname=u.username or u.full_name or "?",
        chat=chat,
        runner=runner,
        label=f"brand_i2i/{model}/{ratio}/{resolution}",
        eta_sec=ETA_BRAND_SEC,
        cleanup_paths=list(init_paths),
        recipe_template=template,
    )
    return ConversationHandler.END


# ── Who is who (Gemini Flash → MJ Imagine) ─────────────────────────────────
# Видим в меню только UID из WIW_UIDS. Пользователь шлёт свободный текст,
# Gemini Flash переписывает по WhoIsWho-промпту, MJ Imagine рисует 4 картинки.
# Под grid'ом — U1-U4 / V1-V4 кнопки + «Повторить» + 👍/👎.
@whitelist_only
async def wiw_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id if update.effective_user else 0
    if uid not in WIW_UIDS:
        # Хотя кнопка и не показывается, защитимся от ручного вызова callback'а.
        _ulog(update, "wiw").warning(f"deny WIW for uid={uid} (not in whitelist)")
        if update.callback_query:
            try:
                await update.callback_query.answer("Недоступно.")
            except Exception:
                pass
        return ConversationHandler.END
    if not await _check_user_capacity(update):
        return ConversationHandler.END
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass
    _ulog(update, "wiw").info("entered prompt-collection state")
    target = _first_message(update)
    await target.reply_text(
        "Пришли набор словосочетаний для Who is who.\n"
        "/cancel — выйти."
    )
    return WIW_PROMPT_STATE


@whitelist_only
async def wiw_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    log = _ulog(update, "wiw")
    uid = update.effective_user.id if update.effective_user else 0
    if uid not in WIW_UIDS:
        log.warning(f"deny WIW prompt for uid={uid}")
        return ConversationHandler.END
    prompt = (update.message.text or "").strip()
    if not prompt:
        log.warning("empty prompt, asking again")
        await update.message.reply_text(
            "Пустое описание. Пришли текст ещё раз или /cancel."
        )
        return WIW_PROMPT_STATE
    log.info(f"prompt received: chars={len(prompt)} prompt={_escape_prompt(prompt)!r}; launching")
    await _wiw_run(update, ctx, prompt)
    return ConversationHandler.END


async def _wiw_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE, prompt: str) -> None:
    state = get_state()
    u = update.effective_user
    chat = update.effective_chat

    async def runner(progress_cb=None, pct_cb=None) -> GenerationJob:
        async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
            return await run_who_is_who(
                client, prompt=prompt,
                progress_cb=progress_cb, pct_cb=pct_cb,
            )

    label = "who-is-who"
    template = TaskRecipe(
        task_uid="", user_id=u.id, label=label,
        workflow="wiw_imagine", prompt=prompt,
        params={},
    )
    await _enqueue_task(
        ctx=ctx,
        uid=u.id,
        uname=u.username or u.full_name or "?",
        chat=chat,
        runner=runner,
        label=label,
        eta_sec=ETA_WIW_SEC,
        recipe_template=template,
    )


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
    uid = update.effective_user.id if update.effective_user else None
    await update.message.reply_text(
        "Главное меню. Выбери, что сделать:",
        reply_markup=_menu_root_kb_for(uid),
    )


@whitelist_only
async def menu_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Роутер callback-кнопок главного меню: переключает подменю «на месте» (edit_message)
    и показывает help. Entry-кнопки сценариев (menu:generate, menu:img2img,
    menu:brand_img2img, menu:brand_photo|render|isometric, menu:prep_speaker)
    разбираются ConversationHandler-ами и сюда не доходят."""
    q = update.callback_query
    action = q.data.split(":", 1)[1]
    await q.answer()
    if action == "root":
        uid = update.effective_user.id if update.effective_user else None
        await q.edit_message_text(
            "Главное меню. Выбери, что сделать:",
            reply_markup=_menu_root_kb_for(uid),
        )
    elif action == "make":
        await q.edit_message_text(
            "Создать новое изображение:",
            reply_markup=_menu_make_kb(),
        )
    elif action == "make_brand":
        await q.edit_message_text(
            "Бренд-вариант для Cloud.ru. У каждого свой Gemini system-prompt:\n"
            "• <b>Photo</b> — фотореализм, люди и сцены.\n"
            "• <b>Render</b> — 3D-объекты и продуктовые рендеры.\n"
            "• <b>2d Isometry</b> — 2D-изометрические сцены и иллюстрации.",
            reply_markup=_menu_make_brand_kb(),
            parse_mode="HTML",
        )
    elif action == "edit":
        await q.edit_message_text(
            "Изменить существующее изображение:",
            reply_markup=_menu_edit_kb(),
        )
    elif action == "help":
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
        await q.answer("Параметры устарели (старше 24 часов).")
        return
    if not await _check_user_capacity_cb(update):
        await q.answer()
        return
    await q.answer("Повторяю с теми же параметрами.")
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
        await q.answer("Параметры устарели (старше 24 часов).")
        return
    await q.answer()
    uid = update.effective_user.id
    state.set_pending_edit(uid, task_uid)
    _ulog(update, "edit-prompt").info(f"pending edit set for task={task_uid}")
    safe = html.escape(recipe.prompt)
    await q.message.reply_text(
        "Текущее описание ниже. Скопируй из код-блока, поправь и пришли следующим сообщением.\n\n"
        f"<pre>{safe}</pre>\n\n"
        "Жду новый текст около 5 минут. /cancel — отменить.\n"
        "Если ты сейчас внутри другого сценария (/generate, /img2img, /prep_speaker), "
        "сначала выйди через /cancel — иначе текст уйдёт туда.",
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
            "Параметры исходной задачи устарели — повтор недоступен."
        )
        return
    new_prompt = (update.message.text or "").strip()
    if not new_prompt:
        await update.message.reply_text("Пустое описание — отменено.")
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
        await q.answer("Картинка устарела или не сохранилась локально.")
        return ConversationHandler.END
    if not await _check_user_capacity(update):
        await q.answer()
        return ConversationHandler.END
    await q.answer("Беру эту картинку как исходник.")

    uid = update.effective_user.id
    user_tmp = state.user_tmp(uid)
    suffix = recipe.result_path.suffix or ".png"
    dst = user_tmp / f"asi2i_{int(time.time() * 1000)}{suffix}"
    try:
        shutil.copyfile(recipe.result_path, dst)
    except Exception as e:
        _ulog(update, "asi2i").warning(f"copy failed: {e!r}")
        await q.message.reply_text("Не смог подготовить картинку. Попробуй ещё раз.")
        return ConversationHandler.END

    ctx.user_data["init_paths"] = [dst]
    _ulog(update, "/img2img").info(f"asi2i: started from task={task_uid}, init={dst.name}")
    await q.message.reply_text(
        "Использую эту картинку как исходник. "
        "Можешь прислать ещё фото (до 4 всего) или сразу отправить /done."
    )
    return I2I_COLLECT


@whitelist_only
async def as_brand_i2i_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """🖼 Использовать результат как init для brand_img2img — entry в conv_brand_i2i.
    Промпт не нужен (фикс. в композере), но collect-фазу оставляем — юзер может добавить
    ещё картинок до /done."""
    q = update.callback_query
    task_uid = q.data.split(":", 1)[1]
    state = get_state()
    recipe = state.get_recipe(task_uid)
    if recipe is None or recipe.result_path is None or not recipe.result_path.exists():
        await q.answer("Картинка устарела или не сохранилась локально.")
        return ConversationHandler.END
    if not await _check_user_capacity(update):
        await q.answer()
        return ConversationHandler.END
    await q.answer("Беру эту картинку для brand img2img.")

    uid = update.effective_user.id
    user_tmp = state.user_tmp(uid)
    suffix = recipe.result_path.suffix or ".png"
    dst = user_tmp / f"asbi2i_{int(time.time() * 1000)}{suffix}"
    try:
        shutil.copyfile(recipe.result_path, dst)
    except Exception as e:
        _ulog(update, "asbi2i").warning(f"copy failed: {e!r}")
        await q.message.reply_text("Не смог подготовить картинку. Попробуй ещё раз.")
        return ConversationHandler.END

    ctx.user_data["init_paths"] = [dst]
    _ulog(update, "/brand_img2img").info(
        f"asbi2i: started from task={task_uid}, init={dst.name}"
    )
    await q.message.reply_text(
        "Использую эту картинку для брендовой генерации (Gemini сам опишет). "
        "Можешь прислать ещё фото (до 4 всего) или сразу отправить /done."
    )
    return BI2I_COLLECT


# ── смена фона у портрета спикера ─────────────────────────────────────────
# spkrbg:<task_uid>:<HEX> — берём result_path исходного prep_speaker (или
# предыдущего speaker_bg) и шлём в Nano Banana как одну init-картинку с
# фикс. промптом «замени фон на сплошной #HEX». Соотношение/разрешение
# наследуем у исходной задачи. Новый recipe.workflow = "speaker_bg" — чтобы
# под результатом снова появились те же 5 цветовых кнопок.
_BG_NAME_BY_HEX: dict[str, str] = {h: name for h, name in BG_COLORS}


@whitelist_only
async def speaker_bg_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parts = (q.data or "").split(":")
    if len(parts) != 3:
        await q.answer("Неверный формат кнопки.")
        return
    _, task_uid, hex_code = parts
    hex_code = hex_code.upper()
    color_name = _BG_NAME_BY_HEX.get(hex_code)
    if color_name is None:
        await q.answer("Неизвестный цвет.")
        return

    state = get_state()
    recipe = state.get_recipe(task_uid)
    if recipe is None or recipe.result_path is None or not recipe.result_path.exists():
        await q.answer("Картинка устарела или не сохранилась локально.")
        return
    if not await _check_user_capacity_cb(update):
        await q.answer()
        return
    await q.answer(f"Меняю фон на {color_name} (#{hex_code}).")

    u = update.effective_user
    chat = update.effective_chat
    uid = u.id
    # Копируем исходник в свежий tmp — _execute_task потом удалит его через cleanup_paths.
    user_tmp = state.user_tmp(uid)
    suffix = recipe.result_path.suffix or ".png"
    src_copy = user_tmp / f"spkrbg_{int(time.time() * 1000)}{suffix}"
    try:
        shutil.copyfile(recipe.result_path, src_copy)
    except Exception as e:
        _ulog(update, "spkrbg").warning(f"copy src failed: {e!r}")
        await chat.send_message("Не смог подготовить картинку. Попробуй ещё раз.")
        return

    prompt = bg_swap_prompt(hex_code, color_name)
    # Наследуем ratio/resolution от исходной задачи — иначе Nano Banana может
    # отдать другой кроп. Дефолты — те же, что у prep_speaker.
    ratio = recipe.params.get("ratio", "r_3_4")
    resolution = recipe.params.get("resolution", "k2")
    model = NANO_BANANA_MODEL
    _ulog(update, "spkrbg").info(
        f"from task={task_uid} → hex={hex_code} name={color_name} "
        f"ratio={ratio} res={resolution}"
    )

    async def runner(progress_cb=None, pct_cb=None) -> GenerationJob:
        async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
            wf = ImageToImageWorkflow(
                client, model_name=model, ratio=ratio, resolution=resolution
            )
            wf.on_progress = pct_cb
            return await wf.run_with_files(prompt=prompt, init_paths=[src_copy])

    template = TaskRecipe(
        task_uid="", user_id=uid,
        label=f"speaker-bg/{hex_code}/{ratio}/{resolution}",
        workflow="speaker_bg", prompt=prompt,
        params={
            "hex": hex_code, "color_name": color_name,
            "ratio": ratio, "resolution": resolution,
            # сохраняем исходник для повторов: см. _rerun_from_recipe (speaker_bg)
            "source_task_uid": task_uid,
        },
    )
    await _enqueue_task(
        ctx=ctx,
        uid=uid,
        uname=u.username or u.full_name or "?",
        chat=chat,
        runner=runner,
        label=f"speaker-bg/{color_name}",
        eta_sec=ETA_NANO_BANANA_SEC,
        cleanup_paths=[src_copy],
        recipe_template=template,
    )


# ── удаление фона у портрета спикера (Photoroom) ──────────────────────────
# spkrrmbg:<task_uid> — берём result_path исходной задачи (portrait / speaker_bg /
# даже speaker_nobg для повторного прогона) и отдаём в Photoroom-ноду.
# Результат — PNG с alpha, отправляем send_document'ом (через as_document=True),
# чтобы Telegram не пожал в JPEG. recipe.workflow="speaker_nobg" → под результатом
# одна кнопка «Удалить фон ещё раз» (см. _action_keyboard).
@whitelist_only
async def speaker_rmbg_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parts = (q.data or "").split(":")
    if len(parts) != 2:
        await q.answer("Неверный формат кнопки.")
        return
    _, task_uid = parts

    state = get_state()
    recipe = state.get_recipe(task_uid)
    if recipe is None:
        await q.answer("Картинка устарела или не сохранилась локально.")
        return

    # Какую картинку прогонять через Photoroom?
    #   - speaker / speaker_bg → result_path (исходный портрет / портрет с цветным фоном)
    #   - speaker_nobg ("ещё раз") → init_paths[0] (тот же src, что был в прошлый раз),
    #     иначе повторно гнали бы уже-без-фона PNG и получали бы то же самое.
    if recipe.workflow == "speaker_nobg" and recipe.init_paths:
        src_for_photoroom = recipe.init_paths[0]
    elif recipe.result_path is not None:
        src_for_photoroom = recipe.result_path
    else:
        await q.answer("Картинка устарела или не сохранилась локально.")
        return
    if not src_for_photoroom.exists():
        await q.answer("Картинка устарела или не сохранилась локально.")
        return

    if not await _check_user_capacity_cb(update):
        await q.answer()
        return
    await q.answer("Удаляю фон…")

    u = update.effective_user
    chat = update.effective_chat
    uid = u.id
    user_tmp = state.user_tmp(uid)
    suffix = src_for_photoroom.suffix or ".png"
    src_copy = user_tmp / f"spkrrmbg_{int(time.time() * 1000)}{suffix}"
    try:
        shutil.copyfile(src_for_photoroom, src_copy)
    except Exception as e:
        _ulog(update, "spkrrmbg").warning(f"copy src failed: {e!r}")
        await chat.send_message("Не смог подготовить картинку. Попробуй ещё раз.")
        return

    _ulog(update, "spkrrmbg").info(f"from task={task_uid} → photoroom")

    async def runner(progress_cb=None, pct_cb=None) -> GenerationJob:
        async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
            wf = PhotoroomBgRemoveWorkflow(client)
            wf.on_progress = pct_cb
            return await wf.run_with_file(init_path=src_copy)

    template = TaskRecipe(
        task_uid="", user_id=uid,
        label="speaker-nobg",
        workflow="speaker_nobg", prompt="",
        params={"source_task_uid": task_uid},
    )
    await _enqueue_task(
        ctx=ctx,
        uid=uid,
        uname=u.username or u.full_name or "?",
        chat=chat,
        runner=runner,
        label="speaker / удалить фон",
        eta_sec=ETA_PHOTOROOM_SEC,
        cleanup_paths=[src_copy],
        recipe_template=template,
    )


@whitelist_only
async def mjact_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """U1-U4 / V1-V4 — Upscale или Variation одной из 4-х картинок MJ-grid'а.

    Callback: `mjact:<U|V>:<task_uid>:<idx>`, где idx ∈ 1..4.
    Берём `recipe.mj_task_id` (он сохранён в _execute_task для wiw_imagine /
    mj_variation) и кидаем `MJUpscaleWorkflow` или `MJVariationWorkflow` с
    `from_task_id=recipe.mj_task_id`, `image_num=idx`.
    """
    q = update.callback_query
    parts = q.data.split(":")
    if len(parts) != 4 or parts[1] not in ("U", "V"):
        await q.answer("Кнопка устарела.")
        return
    op = parts[1]
    task_uid = parts[2]
    try:
        idx = int(parts[3])
    except ValueError:
        await q.answer("Кнопка устарела.")
        return
    if not 1 <= idx <= 4:
        await q.answer("Кнопка устарела.")
        return

    state = get_state()
    recipe = state.get_recipe(task_uid)
    if recipe is None or recipe.mj_task_id is None:
        await q.answer("Параметры устарели (старше 24 часов).")
        return
    if not await _check_user_capacity_cb(update):
        await q.answer()
        return

    u = update.effective_user
    chat = update.effective_chat
    mj_task_id = int(recipe.mj_task_id)
    is_upscale = (op == "U")
    label = f"mj-{'upscale' if is_upscale else 'variation'} {op}{idx}"
    new_workflow = "mj_upscale" if is_upscale else "mj_variation"
    eta = ETA_MJ_UPSCALE_SEC if is_upscale else ETA_MJ_VARIATION_SEC

    await q.answer(f"{op}{idx}: запускаю.")
    _ulog(update, f"mj-{op.lower()}{idx}").info(
        f"from task_uid={task_uid} mj_task_id={mj_task_id}"
    )

    async def runner(progress_cb=None, pct_cb=None) -> GenerationJob:
        async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
            if is_upscale:
                wf = MJUpscaleWorkflow(client, from_task_id=mj_task_id, image_num=idx)
            else:
                wf = MJVariationWorkflow(client, from_task_id=mj_task_id, image_num=idx)
            wf.on_progress = pct_cb
            return await wf.run()

    template = TaskRecipe(
        task_uid="", user_id=u.id, label=label,
        workflow=new_workflow,
        prompt=recipe.prompt,
        params={"image_num": idx, "from_mj_task_id": mj_task_id},
        parent_task_uid=task_uid,
    )
    await _enqueue_task(
        ctx=ctx,
        uid=u.id,
        uname=u.username or u.full_name or "?",
        chat=chat,
        runner=runner,
        label=label,
        eta_sec=eta,
        recipe_template=template,
    )


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
    action = "edit" if prompt_override is not None else "regen"
    logger.bind(uid=u.id, action=action, workflow=recipe.workflow).info(
        f"rerun: workflow={recipe.workflow} chars={len(prompt)} prompt={_escape_prompt(prompt)!r}"
    )

    if recipe.workflow == "nb_t2i":
        model = NANO_BANANA_MODEL
        ratio = params.get("ratio", "default")
        resolution = params.get("resolution", "default")

        async def runner(progress_cb=None, pct_cb=None) -> GenerationJob:
            async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
                wf = ImageGenWorkflow(client, model_name=model, ratio=ratio, resolution=resolution)
                wf.on_progress = pct_cb
            return await wf.run(prompt=prompt)

        label = f"generate/{model}/{ratio}/{resolution}"
        eta = ETA_NANO_BANANA_SEC
        cleanup = []
        new_workflow = "nb_t2i"

    elif recipe.workflow == "nb_i2i":
        model = NANO_BANANA_MODEL
        ratio = params.get("ratio", "r_3_4")
        resolution = params.get("resolution", "default")
        # Готовим init-пути в свежем task_tmp — нельзя пускать regen_cache напрямую,
        # потому что cleanup_paths их потом удалит после задачи.
        init_paths = _stage_inits_from_recipe(state, u.id, recipe)

        async def runner(progress_cb=None, pct_cb=None) -> GenerationJob:
            async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
                wf = ImageToImageWorkflow(client, model_name=model, ratio=ratio, resolution=resolution)
                wf.on_progress = pct_cb
            return await wf.run_with_files(prompt=prompt, init_paths=init_paths)

        label = f"img2img/{model}/{ratio}/{resolution}"
        eta = ETA_NANO_BANANA_SEC
        cleanup = list(init_paths)
        new_workflow = "nb_i2i"

    elif recipe.workflow == "brand_t2i":
        model = NANO_BANANA_MODEL
        ratio = params.get("ratio", "default")
        resolution = params.get("resolution", "default")
        variant = params.get("variant", "photo")

        async def runner(progress_cb=None, pct_cb=None) -> GenerationJob:
            async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
                return await run_brand_text2img(
                    client, prompt=prompt, variant=variant, model_name=model,
                    ratio=ratio, resolution=resolution,
                    progress_cb=progress_cb, pct_cb=pct_cb,
                )

        label = f"brand_t2i:{variant}/{model}/{ratio}/{resolution}"
        eta = ETA_BRAND_SEC
        cleanup = []
        new_workflow = "brand_t2i"

    elif recipe.workflow == "brand_i2i":
        model = NANO_BANANA_MODEL
        ratio = params.get("ratio", "r_3_4")
        resolution = params.get("resolution", "default")
        # init-картинки переезжают в свежий task_tmp, чтобы cleanup_paths их потом удалил.
        init_paths = _stage_inits_from_recipe(state, u.id, recipe)
        if not init_paths:
            await chat.send_message("Не нашёл локальной копии исходников для повтора.")
            return

        async def runner(progress_cb=None, pct_cb=None) -> GenerationJob:
            async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
                return await run_brand_img2img(
                    client, init_paths=init_paths,
                    model_name=model, ratio=ratio, resolution=resolution,
                    progress_cb=progress_cb, pct_cb=pct_cb,
                )

        label = f"brand_i2i/{model}/{ratio}/{resolution}"
        eta = ETA_BRAND_SEC
        cleanup = list(init_paths)
        new_workflow = "brand_i2i"

    elif recipe.workflow == "speaker":
        gender = params.get("gender", "man")
        ratio = params.get("ratio", "r_3_4")
        resolution = params.get("resolution", "k2")
        # Спикер-фото — второй элемент init_paths (первый был DEFAULT_REFERENCE).
        # Но в recipe.init_paths мы сохранили только cleanup_paths, т.е. без DEFAULT_REFERENCE.
        init_paths = _stage_inits_from_recipe(state, u.id, recipe)
        if not init_paths:
            await chat.send_message("Не нашёл локальной копии фото спикера для повтора.")
            return
        speaker_photo = init_paths[0]
        # Если ✏️ Уточнить вызвал regen — prompt в recipe собран из speaker_prompt(gender);
        # юзерский override применяем как есть (он же и редактирует этот системный промпт).

        async def runner(progress_cb=None, pct_cb=None) -> GenerationJob:
            async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
                wf = build_speaker_prep_workflow(client)
                wf.ratio = ratio
                wf.resolution = resolution
                wf.on_progress = pct_cb
            return await wf.run_with_files(
                    prompt=prompt, init_paths=[DEFAULT_REFERENCE, speaker_photo]
                )

        label = f"prep-speaker/{gender}/{ratio}/{resolution}"
        eta = ETA_NANO_BANANA_SEC
        cleanup = [speaker_photo]
        new_workflow = "speaker"

    elif recipe.workflow == "speaker_bg":
        # Повторяем смену фона: тот же исходник из recipe.init_paths,
        # тот же prompt (с тем же hex) — Nano Banana отдаст новый вариант.
        model = NANO_BANANA_MODEL
        ratio = params.get("ratio", "r_3_4")
        resolution = params.get("resolution", "k2")
        init_paths = _stage_inits_from_recipe(state, u.id, recipe)
        if not init_paths:
            await chat.send_message("Не нашёл локальной копии исходника для повтора.")
            return

        async def runner(progress_cb=None, pct_cb=None) -> GenerationJob:
            async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
                wf = ImageToImageWorkflow(
                    client, model_name=model, ratio=ratio, resolution=resolution
                )
                wf.on_progress = pct_cb
            return await wf.run_with_files(prompt=prompt, init_paths=init_paths)

        color_name = params.get("color_name", "bg")
        label = f"speaker-bg/{color_name}"
        eta = ETA_NANO_BANANA_SEC
        cleanup = list(init_paths)
        new_workflow = "speaker_bg"

    elif recipe.workflow == "speaker_nobg":
        # Повторяем удаление фона: используем тот же src (предыдущий result_path
        # уже не годится — он PNG без фона). init_paths хранит исходник, который
        # был отправлен в Photoroom первый раз.
        init_paths = _stage_inits_from_recipe(state, u.id, recipe)
        if not init_paths:
            await chat.send_message("Не нашёл локальной копии исходника для повтора.")
            return
        src = init_paths[0]

        async def runner(progress_cb=None, pct_cb=None) -> GenerationJob:
            async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
                wf = PhotoroomBgRemoveWorkflow(client)
                wf.on_progress = pct_cb
            return await wf.run_with_file(init_path=src)

        label = "speaker-nobg"
        eta = ETA_PHOTOROOM_SEC
        cleanup = list(init_paths)
        new_workflow = "speaker_nobg"

    elif recipe.workflow == "wiw_imagine":
        # Повторить Who is who — тот же user-prompt, новый прогон Gemini Flash + MJ
        # (получим другую сидку — это и есть смысл «Повторить» под grid'ом).
        async def runner(progress_cb=None, pct_cb=None) -> GenerationJob:
            async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
                return await run_who_is_who(
                    client, prompt=prompt,
                    progress_cb=progress_cb, pct_cb=pct_cb,
                )

        label = "who-is-who"
        eta = ETA_WIW_SEC
        cleanup = []
        new_workflow = "wiw_imagine"

    elif recipe.workflow in ("mj_upscale", "mj_variation"):
        # Повторить U/V — нужен исходный mj_task_id из ПРЕДКА (parent_task_uid),
        # потому что MJ child-ноды не цепляются к собственному task_id.
        from_mj = (
            recipe.params.get("from_mj_task_id")
            if isinstance(recipe.params, dict) else None
        )
        image_num = (
            recipe.params.get("image_num") if isinstance(recipe.params, dict) else None
        )
        if not isinstance(from_mj, int) or not isinstance(image_num, int):
            await chat.send_message(
                "Не нашёл параметров MJ-задачи для повтора (нет mj_task_id/image_num)."
            )
            return
        is_upscale = recipe.workflow == "mj_upscale"

        async def runner(progress_cb=None, pct_cb=None) -> GenerationJob:
            async with PhygitalClient(state.session, session_manager=state.session_manager) as client:
                if is_upscale:
                    wf = MJUpscaleWorkflow(client, from_task_id=from_mj, image_num=image_num)
                else:
                    wf = MJVariationWorkflow(client, from_task_id=from_mj, image_num=image_num)
                wf.on_progress = pct_cb
                return await wf.run()

        label = f"mj-{'upscale' if is_upscale else 'variation'} {('U' if is_upscale else 'V')}{image_num}"
        eta = ETA_MJ_UPSCALE_SEC if is_upscale else ETA_MJ_VARIATION_SEC
        cleanup = []
        new_workflow = recipe.workflow

    else:
        await chat.send_message(
            f"Не знаю, как повторить этот тип задачи (workflow={recipe.workflow})."
        )
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
    """Пять ConversationHandler по числу сценариев:
      - generate / img2img / prep_speaker / brand_i2i — entry только из меню.
      - brand_t2i — entry из меню по трём вариантам (photo/render/isometric).
    Img2img дополнительно слушает `asi2i:*` (Изменить изображение на результате),
    brand_i2i — `asbi2i:*` (Добавить Brand patterns на результате).
    Fallbacks: inline-Cancel (callback `cancel:*`) и /cancel."""
    img_filter = filters.PHOTO | (filters.Document.IMAGE)
    cancel_handlers = [
        CommandHandler("cancel", cmd_cancel),
        CallbackQueryHandler(cmd_cancel, pattern=r"^cancel:"),
    ]

    conv_generate = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(gen_start, pattern=r"^menu:generate$"),
        ],
        states={
            GEN_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, gen_prompt)],
            GEN_RATIO: [CallbackQueryHandler(gen_ratio, pattern=r"^ratio:")],
            GEN_RES: [CallbackQueryHandler(gen_res, pattern=r"^res:")],
        },
        fallbacks=cancel_handlers,
        name="generate",
        persistent=False,
    )

    conv_img2img = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(i2i_start, pattern=r"^menu:img2img$"),
            CallbackQueryHandler(as_i2i_cb, pattern=r"^asi2i:"),
        ],
        states={
            I2I_COLLECT: [
                MessageHandler(img_filter, i2i_photo),
                CommandHandler("done", i2i_done),
            ],
            I2I_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, i2i_prompt)],
            I2I_RATIO: [CallbackQueryHandler(i2i_ratio, pattern=r"^ratio:")],
            I2I_RES: [CallbackQueryHandler(i2i_res, pattern=r"^res:")],
        },
        fallbacks=cancel_handlers,
        name="img2img",
        persistent=False,
    )

    conv_prep = ConversationHandler(
        entry_points=[
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

    conv_brand_t2i = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                bt2i_start,
                pattern=r"^menu:brand_(photo|render|isometric)$",
            ),
        ],
        states={
            BT2I_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, bt2i_prompt)],
            BT2I_RATIO: [CallbackQueryHandler(bt2i_ratio, pattern=r"^ratio:")],
            BT2I_RES: [CallbackQueryHandler(bt2i_res, pattern=r"^res:")],
        },
        fallbacks=cancel_handlers,
        name="brand_generate",
        persistent=False,
    )

    conv_brand_i2i = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(bi2i_start, pattern=r"^menu:brand_img2img$"),
            CallbackQueryHandler(as_brand_i2i_cb, pattern=r"^asbi2i:"),
        ],
        states={
            BI2I_COLLECT: [
                MessageHandler(img_filter, bi2i_photo),
                CommandHandler("done", bi2i_done),
            ],
            BI2I_RATIO: [CallbackQueryHandler(bi2i_ratio, pattern=r"^ratio:")],
            BI2I_RES: [CallbackQueryHandler(bi2i_res, pattern=r"^res:")],
        },
        fallbacks=cancel_handlers,
        name="brand_img2img",
        persistent=False,
    )

    # Who is who — экспериментальный сценарий для UID из WIW_UIDS (см. wiw_start).
    # Entry скрыт от других юзеров через _menu_root_kb_for, а wiw_start ещё раз
    # проверяет UID на случай прямого вызова callback'а.
    conv_wiw = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(wiw_start, pattern=r"^menu:wiw$"),
        ],
        states={
            WIW_PROMPT_STATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wiw_prompt),
            ],
        },
        fallbacks=cancel_handlers,
        name="who_is_who",
        persistent=False,
    )

    return [conv_generate, conv_img2img, conv_prep, conv_brand_t2i, conv_brand_i2i, conv_wiw]


def build_extra_handlers() -> list:
    """Стандалон-хендлеры, регистрируются после ConversationHandler:
      - /menu — открыть главное меню (одна команда для всех сценариев).
      - menu_router — навигация корень/подменю + show help. Entry-кнопки
        сценариев (menu:generate, menu:img2img, menu:brand_img2img,
        menu:brand_{photo,render,isometric}, menu:prep_speaker) сюда не доходят —
        их забирают ConversationHandler.
      - regen_cb / edit_prompt_cb — на инлайн-кнопках готового результата.
      - pending_edit_listener — глобальный MessageHandler в group=1, чтобы НЕ
        перехватывать текст активной conversation (она в group=0 заберёт первой).
    """
    return [
        ("group0", CommandHandler("menu", cmd_menu)),
        # Точный список callback'ов навигации — не должен пересекаться с entry-точками conv'ов.
        ("group0", CallbackQueryHandler(
            menu_router, pattern=r"^menu:(root|make|make_brand|edit|help)$"
        )),
        ("group0", CallbackQueryHandler(regen_cb, pattern=r"^regen:")),
        ("group0", CallbackQueryHandler(edit_prompt_cb, pattern=r"^editp:")),
        ("group0", CallbackQueryHandler(speaker_bg_cb, pattern=r"^spkrbg:")),
        ("group0", CallbackQueryHandler(speaker_rmbg_cb, pattern=r"^spkrrmbg:")),
        # U1-U4 / V1-V4 под MJ-grid'ом (wiw_imagine, mj_variation).
        ("group0", CallbackQueryHandler(mjact_cb, pattern=r"^mjact:")),
        # Подсистема обратной связи: menu:feedback + все fb:* callback'и + listener текста.
        *build_feedback_handlers(),
        # Глобальный listener в group=1 — после conversations.
        ("group1", MessageHandler(filters.TEXT & ~filters.COMMAND, pending_edit_listener)),
    ]
