"""Сбор обратной связи: per-result рейтинг, опрос v1, свободные комментарии.

Точки входа (все — из главного меню, командных шорткатов нет):
  • меню → «Обратная связь» → подменю
      ├ «Пройти опрос» / «Пройти опрос ещё раз»
      ├ «Оставить комментарий»
      └ «Статистика»  (только для uid из settings.owner_user_ids)
  • под каждой картинкой — кнопки 👍 / 👎 в `_action_keyboard`.
  • один раз после 3-й успешной генерации — мягкий баннер с предложением пройти опрос.

Хранение:
  storage/feedback.jsonl       — append-only события (rating / comment / survey)
  storage/feedback_state.json  — per-uid состояние (счётчики, флаги)

В обоих файлах хранятся только uid (числовой) и пользовательский ввод.
Username / имя / chat_id не пишутся.

In-progress опрос/комментарий живут в `ctx.user_data["fb_*"]` (волатильно,
теряется при рестарте бота — приемлемо: юзер заново начинает с первого вопроса).
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from loguru import logger
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes

from bot.auth import whitelist_only
from bot.feedback_questions import (
    COMMENT_CATEGORIES,
    RATING_REASONS,
    SURVEY_QUESTIONS,
    SURVEY_VERSION,
    Question,
)
from client.config import ROOT, settings

# ── пути и константы ───────────────────────────────────────────────────────
FEEDBACK_DIR = ROOT / "storage"
EVENTS_FILE = FEEDBACK_DIR / "feedback.jsonl"
STATE_FILE = FEEDBACK_DIR / "feedback_state.json"

# Авто-prompt после N-й успешной генерации (первый показ).
AUTO_PROMPT_AFTER_GENERATIONS = 3
# Минимум 7 дней между повторными показами авто-prompt'а одному и тому же юзеру.
AUTO_PROMPT_COOLDOWN_SEC = 7 * 24 * 3600
# После N-го «Не сейчас» — молча перестаём предлагать (до выхода новой версии).
AUTO_PROMPT_MAX_LATER = 5

# Максимальная длина свободного текста (комментарии, ответы на free-вопросы).
MAX_FREE_TEXT_LEN = 2000


# ── модель состояния (in-memory + persist) ─────────────────────────────────
def _default_uid_state() -> dict[str, Any]:
    return {
        "survey_completed_version": None,  # None | "v1" | "v1_declined"
        "auto_prompt_count": 0,
        "last_prompt_ts": 0,
        "successful_generations": 0,
        "ratings_total": 0,
        "comments_total": 0,
        "surveys_total": 0,
    }


class FeedbackStore:
    """Singleton — держит per-uid состояние в памяти, персистит в JSON.
    События пишет append-only в JSONL.

    Все операции синхронные (файлы маленькие, fsync в норме <1ms).
    Под параллельный доступ — asyncio.Lock на запись state и jsonl.
    """

    def __init__(self) -> None:
        FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
        self._state: dict[str, dict[str, Any]] = {}
        self._events_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._load_state()

    def _load_state(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            raw = STATE_FILE.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
            if isinstance(data, dict):
                # Достраиваем недостающие поля (на случай старой схемы).
                for uid, st in data.items():
                    merged = _default_uid_state()
                    if isinstance(st, dict):
                        merged.update(st)
                    self._state[str(uid)] = merged
        except Exception as e:
            logger.warning(f"feedback state load failed: {type(e).__name__}: {e}")

    async def _persist_state(self) -> None:
        async with self._state_lock:
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(STATE_FILE)

    async def _append_event(self, event: dict[str, Any]) -> None:
        async with self._events_lock:
            line = json.dumps(event, ensure_ascii=False)
            with EVENTS_FILE.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def _ensure_uid(self, uid: int) -> dict[str, Any]:
        key = str(uid)
        if key not in self._state:
            self._state[key] = _default_uid_state()
        return self._state[key]

    # ── публичный API ─────────────────────────────────────────────────────
    def get_uid_state(self, uid: int) -> dict[str, Any]:
        return self._ensure_uid(uid).copy()

    async def mark_generation_success(self, uid: int) -> int:
        """Инкремент счётчика успешных генераций. Возвращает новое значение."""
        st = self._ensure_uid(uid)
        st["successful_generations"] += 1
        await self._persist_state()
        return st["successful_generations"]

    async def record_rating(
        self,
        *,
        uid: int,
        task_uid: str,
        workflow: str,
        value: str,
        reasons: list[str] | None = None,
        comment: str = "",
    ) -> None:
        st = self._ensure_uid(uid)
        st["ratings_total"] += 1
        event = {
            "type": "rating",
            "ts": int(time.time()),
            "uid": uid,
            "task_uid": task_uid,
            "workflow": workflow,
            "value": value,
            "reasons": reasons or [],
            "comment": _trim(comment),
        }
        await self._append_event(event)
        await self._persist_state()

    async def record_comment(
        self,
        *,
        uid: int,
        category: str,
        text: str,
    ) -> None:
        st = self._ensure_uid(uid)
        st["comments_total"] += 1
        event = {
            "type": "comment",
            "ts": int(time.time()),
            "uid": uid,
            "category": category,
            "text": _trim(text),
        }
        await self._append_event(event)
        await self._persist_state()

    async def record_survey(
        self,
        *,
        uid: int,
        answers: dict[str, Any],
        skipped: list[str],
        duration_sec: int,
    ) -> None:
        st = self._ensure_uid(uid)
        st["surveys_total"] += 1
        st["survey_completed_version"] = SURVEY_VERSION
        event = {
            "type": "survey",
            "ts": int(time.time()),
            "uid": uid,
            "survey_version": SURVEY_VERSION,
            "answers": answers,
            "skipped": skipped,
            "duration_sec": duration_sec,
        }
        await self._append_event(event)
        await self._persist_state()

    async def mark_prompt_shown(self, uid: int) -> None:
        st = self._ensure_uid(uid)
        st["auto_prompt_count"] += 1
        st["last_prompt_ts"] = int(time.time())
        await self._persist_state()

    async def mark_prompt_declined(self, uid: int) -> None:
        st = self._ensure_uid(uid)
        st["survey_completed_version"] = f"{SURVEY_VERSION}_declined"
        await self._persist_state()

    def should_auto_prompt(self, uid: int) -> bool:
        """Подходит ли момент для авто-prompt'а опроса.

        Условия (все):
          - юзер не прошёл и не отказался от текущей версии опроса;
          - сделал ≥ AUTO_PROMPT_AFTER_GENERATIONS успешных генераций;
          - не превысил лимит «не сейчас»-отказов (AUTO_PROMPT_MAX_LATER);
          - с прошлого показа прошло ≥ AUTO_PROMPT_COOLDOWN_SEC.
        """
        st = self._ensure_uid(uid)
        if st["survey_completed_version"] is not None:
            return False
        if st["successful_generations"] < AUTO_PROMPT_AFTER_GENERATIONS:
            return False
        if st["auto_prompt_count"] >= AUTO_PROMPT_MAX_LATER:
            return False
        if st["last_prompt_ts"] and time.time() - st["last_prompt_ts"] < AUTO_PROMPT_COOLDOWN_SEC:
            return False
        return True

    # ── админ-стата ──────────────────────────────────────────────────────
    def compute_stats(self) -> str:
        """Читает feedback.jsonl полностью, считает агрегаты, возвращает markdown."""
        if not EVENTS_FILE.exists():
            return "Пока нет данных — никто ничего не оставлял."
        ratings_up = 0
        ratings_down = 0
        reason_counter: Counter[str] = Counter()
        comments: list[tuple[int, str, str]] = []  # (ts, category, text)
        surveys: list[dict] = []
        try:
            with EVENTS_FILE.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    t = ev.get("type")
                    if t == "rating":
                        if ev.get("value") == "up":
                            ratings_up += 1
                        else:
                            ratings_down += 1
                        for r in ev.get("reasons", []):
                            reason_counter[r] += 1
                    elif t == "comment":
                        comments.append((ev.get("ts", 0), ev.get("category", "?"), ev.get("text", "")))
                    elif t == "survey":
                        surveys.append(ev)
        except Exception as e:
            return f"Ошибка чтения статистики: {type(e).__name__}: {e}"

        lines = [
            f"<b>Статистика обратной связи</b>",
            "",
            f"Рейтинги: 👍 {ratings_up} / 👎 {ratings_down}",
        ]
        if reason_counter:
            top_reasons = ", ".join(f"{k}:{v}" for k, v in reason_counter.most_common(5))
            lines.append(f"Топ причин 👎: {top_reasons}")
        lines.append(f"Опросов завершено: {len(surveys)}")
        lines.append(f"Комментариев: {len(comments)}")
        if comments:
            lines.append("")
            lines.append("<b>Последние 5 комментариев:</b>")
            for ts, cat, text in sorted(comments, key=lambda x: x[0], reverse=True)[:5]:
                preview = text[:200].replace("<", "&lt;").replace(">", "&gt;")
                lines.append(f"• [{cat}] {preview}")
        return "\n".join(lines)


def _trim(text: str) -> str:
    text = (text or "").strip()
    return text[:MAX_FREE_TEXT_LEN]


_store: FeedbackStore | None = None


def get_feedback_store() -> FeedbackStore:
    global _store
    if _store is None:
        _store = FeedbackStore()
    return _store


def reset_feedback_store_for_tests() -> None:
    global _store
    _store = None


# ═══════════════════════════════════════════════════════════════════════════
# UI: клавиатуры
# ═══════════════════════════════════════════════════════════════════════════
def feedback_root_kb(*, is_owner: bool, survey_already_done: bool) -> InlineKeyboardMarkup:
    survey_label = "Пройти опрос ещё раз" if survey_already_done else "Пройти опрос"
    rows = [
        [InlineKeyboardButton(survey_label, callback_data="fb:menu:survey")],
        [InlineKeyboardButton("Оставить комментарий", callback_data="fb:menu:comment")],
    ]
    if is_owner:
        rows.append([InlineKeyboardButton("Статистика", callback_data="fb:menu:stats")])
    rows.append([InlineKeyboardButton("Назад", callback_data="menu:root")])
    return InlineKeyboardMarkup(rows)


def rating_kb(task_uid: str, workflow: str) -> list[InlineKeyboardButton]:
    """Возвращает строку из двух кнопок 👍/👎 для action_keyboard.
    Возвращаем именно ряд (list), чтобы scenarios.py смог встроить в свою сетку."""
    return [
        InlineKeyboardButton("👍", callback_data=f"fb:rate:{task_uid}:{workflow}:up"),
        InlineKeyboardButton("👎", callback_data=f"fb:rate:{task_uid}:{workflow}:down"),
    ]


def auto_prompt_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Пройти", callback_data="fb:auto:take")],
        [
            InlineKeyboardButton("Не сейчас", callback_data="fb:auto:later"),
            InlineKeyboardButton("Не показывать", callback_data="fb:auto:never"),
        ],
    ])


# ═══════════════════════════════════════════════════════════════════════════
# UI: рендер вопроса опроса
# ═══════════════════════════════════════════════════════════════════════════
def _question_by_id(qid: str) -> Question | None:
    for q in SURVEY_QUESTIONS:
        if q.id == qid:
            return q
    return None


def _render_question(q: Question, step_idx: int, total: int, selected: set[str]) -> tuple[str, InlineKeyboardMarkup]:
    """Возвращает (text, keyboard) для отображения вопроса."""
    progress = f"Вопрос {step_idx + 1}/{total}"
    text = f"<b>{progress}</b>\n\n{q.text}"
    rows: list[list[InlineKeyboardButton]] = []
    if q.kind == "multi":
        # toggle-кнопки + «Дальше» + «Пропустить»
        for value, label in q.options:
            mark = "✓ " if value in selected else ""
            rows.append([InlineKeyboardButton(
                f"{mark}{label}", callback_data=f"fb:srv:toggle:{value}"
            )])
        if q.allow_none:
            mark = "✓ " if q.none_value in selected else ""
            rows.append([InlineKeyboardButton(
                f"{mark}{q.none_label}", callback_data=f"fb:srv:toggle:{q.none_value}"
            )])
        rows.append([
            InlineKeyboardButton("Дальше", callback_data="fb:srv:next"),
            InlineKeyboardButton("Пропустить", callback_data="fb:srv:skip"),
        ])
    elif q.kind in ("single", "single_free_on"):
        for value, label in q.options:
            rows.append([InlineKeyboardButton(label, callback_data=f"fb:srv:pick:{value}")])
        rows.append([InlineKeyboardButton("Пропустить", callback_data="fb:srv:skip")])
    elif q.kind == "scale":
        rows.append([
            InlineKeyboardButton(str(n), callback_data=f"fb:srv:scale:{n}")
            for n in range(1, 6)
        ])
        if q.allow_none:
            rows.append([InlineKeyboardButton(q.none_label, callback_data=f"fb:srv:scale:{q.none_value}")])
        rows.append([InlineKeyboardButton("Пропустить", callback_data="fb:srv:skip")])
    elif q.kind == "free":
        text += "\n\n<i>Напиши одно сообщение в ответ.</i>"
        rows.append([InlineKeyboardButton("Пропустить", callback_data="fb:srv:skip")])
    rows.append([InlineKeyboardButton("Отменить опрос", callback_data="fb:srv:cancel")])
    return text, InlineKeyboardMarkup(rows)


def _comment_categories_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(label, callback_data=f"fb:cmt:{value}")]
        for value, label in COMMENT_CATEGORIES
    ]
    rows.append([InlineKeyboardButton("Отмена", callback_data="fb:cmt:cancel")])
    return InlineKeyboardMarkup(rows)


def _reasons_kb(task_uid: str, workflow: str, selected: set[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    # По 2 в ряд для компактности.
    row: list[InlineKeyboardButton] = []
    for value, label in RATING_REASONS:
        mark = "✓ " if value in selected else ""
        row.append(InlineKeyboardButton(
            f"{mark}{label}",
            callback_data=f"fb:rsn:{task_uid}:{workflow}:toggle:{value}",
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("Готово", callback_data=f"fb:rsn:{task_uid}:{workflow}:done"),
        InlineKeyboardButton("Без причин", callback_data=f"fb:rsn:{task_uid}:{workflow}:skip"),
    ])
    return InlineKeyboardMarkup(rows)


def _post_reasons_kb(task_uid: str, workflow: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Добавить комментарий", callback_data=f"fb:rsn:{task_uid}:{workflow}:comment"),
        InlineKeyboardButton("Пропустить", callback_data=f"fb:rsn:{task_uid}:{workflow}:nocomment"),
    ]])


# ═══════════════════════════════════════════════════════════════════════════
# Handlers
# ═══════════════════════════════════════════════════════════════════════════
@whitelist_only
async def feedback_menu_cb(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Вход в подменю «Обратная связь» из главного меню."""
    q = update.callback_query
    uid = update.effective_user.id
    await q.answer()
    store = get_feedback_store()
    st = store.get_uid_state(uid)
    survey_already_done = st["survey_completed_version"] in (SURVEY_VERSION, f"{SURVEY_VERSION}_declined")
    is_owner = uid in settings.owner_user_ids
    text = "Обратная связь:"
    if st["survey_completed_version"] == SURVEY_VERSION:
        text += "\n\nТы уже проходил опрос — спасибо. Можно пройти ещё раз: новый ответ будет учтён рядом со старым."
    await q.edit_message_text(
        text,
        reply_markup=feedback_root_kb(
            is_owner=is_owner, survey_already_done=survey_already_done,
        ),
    )


@whitelist_only
async def feedback_action_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Роутер действий из подменю обратной связи: survey / comment / stats."""
    q = update.callback_query
    action = q.data.split(":", 2)[2]  # fb:menu:<action>
    uid = update.effective_user.id
    await q.answer()
    if action == "survey":
        await _start_survey(update, ctx)
    elif action == "comment":
        ctx.user_data.pop("fb_pending", None)
        await q.edit_message_text(
            "Что хочешь оставить?",
            reply_markup=_comment_categories_kb(),
        )
    elif action == "stats":
        if uid not in settings.owner_user_ids:
            await q.edit_message_text("Доступно только для owner-uid.")
            return
        text = get_feedback_store().compute_stats()
        await q.edit_message_text(text, parse_mode="HTML")


# ── survey flow ────────────────────────────────────────────────────────────
async def _start_survey(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Начать опрос с первого вопроса. Сбрасывает любое прошлое in-progress состояние."""
    q = update.callback_query
    ctx.user_data["fb_survey"] = {
        "step": 0,
        "answers": {},
        "skipped": [],
        "started_ts": int(time.time()),
        "pending_multi": set(),
    }
    ctx.user_data.pop("fb_pending", None)
    await _render_current_step(update, ctx)
    _ulog(update, "survey:start").info("survey started")


async def _render_current_step(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Перерисовать сообщение под текущий шаг (или показать summary, если опрос закончен)."""
    q = update.callback_query
    surv = ctx.user_data.get("fb_survey")
    if not surv:
        return
    step = surv["step"]
    total = len(SURVEY_QUESTIONS)
    if step >= total:
        await _show_survey_summary(update, ctx)
        return
    qst = SURVEY_QUESTIONS[step]
    selected = surv.get("pending_multi", set()) if qst.kind == "multi" else set()
    text, kb = _render_question(qst, step, total, selected)
    if qst.kind == "free":
        # Включаем флаг — следующий текст юзера идёт в этот ответ.
        ctx.user_data["fb_pending"] = {"kind": "survey_free", "qid": qst.id}
    else:
        ctx.user_data.pop("fb_pending", None)
    try:
        await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        # Сообщение могло протухнуть — отправим новое.
        await q.message.reply_text(text, reply_markup=kb, parse_mode="HTML")


async def _show_survey_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    surv = ctx.user_data.get("fb_survey", {})
    answered = len(surv.get("answers", {}))
    skipped = len(surv.get("skipped", []))
    text = (
        f"Опрос пройден: {answered} ответов, {skipped} пропусков.\n"
        f"Отправить?"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Отправить", callback_data="fb:srv:submit"),
        InlineKeyboardButton("Отменить", callback_data="fb:srv:cancel"),
    ]])
    try:
        await q.edit_message_text(text, reply_markup=kb)
    except Exception:
        await q.message.reply_text(text, reply_markup=kb)


def _advance_step(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Сдвигает шаг на 1 + чистит pending_multi."""
    surv = ctx.user_data.get("fb_survey")
    if not surv:
        return
    surv["step"] += 1
    surv["pending_multi"] = set()


@whitelist_only
async def survey_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Все callback'и опроса: fb:srv:<action>[:<value>]."""
    q = update.callback_query
    parts = q.data.split(":")
    # fb:srv:<action>[:<value>...]
    action = parts[2]
    surv = ctx.user_data.get("fb_survey")
    if not surv and action != "cancel":
        await q.answer("Опрос уже закрыт. Открой меню → Обратная связь.")
        return

    if action == "cancel":
        ctx.user_data.pop("fb_survey", None)
        ctx.user_data.pop("fb_pending", None)
        await q.answer("Опрос отменён.")
        try:
            await q.edit_message_text("Опрос отменён. Ничего не сохранено.")
        except Exception:
            pass
        return

    step = surv["step"]
    qst = SURVEY_QUESTIONS[step] if step < len(SURVEY_QUESTIONS) else None

    if action == "skip" and qst is not None:
        surv["skipped"].append(qst.id)
        ctx.user_data.pop("fb_pending", None)
        _advance_step(ctx)
        await q.answer("Пропущено.")
        await _render_current_step(update, ctx)
        return

    if action == "toggle" and qst is not None and qst.kind == "multi":
        value = parts[3]
        sel = surv.setdefault("pending_multi", set())
        if value in sel:
            sel.remove(value)
        else:
            sel.add(value)
        await q.answer()
        await _render_current_step(update, ctx)
        return

    if action == "next" and qst is not None and qst.kind == "multi":
        sel = list(surv.get("pending_multi", set()))
        if not sel:
            surv["skipped"].append(qst.id)
        else:
            surv["answers"][qst.id] = sel
        _advance_step(ctx)
        await q.answer()
        await _render_current_step(update, ctx)
        return

    if action == "pick" and qst is not None and qst.kind in ("single", "single_free_on"):
        value = parts[3]
        surv["answers"][qst.id] = value
        await q.answer()
        # single_free_on с триггерным значением → перед advance показать free-prompt экран
        if qst.kind == "single_free_on" and qst.free_on == value:
            ctx.user_data["fb_pending"] = {"kind": "survey_free", "qid": f"{qst.id}_free"}
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("Пропустить", callback_data="fb:srv:skip_free"),
                InlineKeyboardButton("Отменить опрос", callback_data="fb:srv:cancel"),
            ]])
            try:
                await q.edit_message_text(qst.free_prompt, reply_markup=kb)
            except Exception:
                await q.message.reply_text(qst.free_prompt, reply_markup=kb)
            return
        _advance_step(ctx)
        await _render_current_step(update, ctx)
        return

    if action == "scale" and qst is not None and qst.kind == "scale":
        value = parts[3]
        surv["answers"][qst.id] = value
        await q.answer()
        # follow_up_free — отдельный экран, только если выбрано числовое значение
        is_numeric = value.isdigit()
        if qst.follow_up_free and is_numeric:
            ctx.user_data["fb_pending"] = {"kind": "survey_free", "qid": f"{qst.id}_free"}
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("Пропустить", callback_data="fb:srv:skip_free"),
                InlineKeyboardButton("Отменить опрос", callback_data="fb:srv:cancel"),
            ]])
            try:
                await q.edit_message_text(qst.free_prompt, reply_markup=kb)
            except Exception:
                await q.message.reply_text(qst.free_prompt, reply_markup=kb)
            return
        _advance_step(ctx)
        await _render_current_step(update, ctx)
        return

    if action == "skip_free":
        # Free follow-up пропущен — сдвигаемся вперёд.
        ctx.user_data.pop("fb_pending", None)
        _advance_step(ctx)
        await q.answer("Пропущено.")
        await _render_current_step(update, ctx)
        return

    if action == "submit":
        await _finalize_survey(update, ctx)
        return

    await q.answer()


async def _finalize_survey(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    surv = ctx.user_data.pop("fb_survey", None)
    ctx.user_data.pop("fb_pending", None)
    if not surv:
        return
    uid = update.effective_user.id
    duration = max(0, int(time.time()) - int(surv.get("started_ts", time.time())))
    await get_feedback_store().record_survey(
        uid=uid,
        answers=surv["answers"],
        skipped=surv["skipped"],
        duration_sec=duration,
    )
    _ulog(update, "survey:submit").info(
        f"survey {SURVEY_VERSION} submitted: answers={len(surv['answers'])} "
        f"skipped={len(surv['skipped'])} dur={duration}s"
    )
    try:
        await q.edit_message_text("Спасибо! Ответы сохранены.")
    except Exception:
        await q.message.reply_text("Спасибо! Ответы сохранены.")


# ── comment flow ──────────────────────────────────────────────────────────
@whitelist_only
async def comment_category_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """fb:cmt:<category> — выбрана категория, ждём текст."""
    q = update.callback_query
    parts = q.data.split(":", 2)
    category = parts[2]
    if category == "cancel":
        ctx.user_data.pop("fb_pending", None)
        await q.answer("Отменено.")
        try:
            await q.edit_message_text("Отменено.")
        except Exception:
            pass
        return
    valid = {v for v, _ in COMMENT_CATEGORIES}
    if category not in valid:
        await q.answer("Неизвестная категория.")
        return
    ctx.user_data["fb_pending"] = {"kind": "comment", "category": category}
    await q.answer()
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Отмена", callback_data="fb:cmt:cancel"),
    ]])
    try:
        await q.edit_message_text(
            "Напиши одно сообщение — отправлю как комментарий.",
            reply_markup=kb,
        )
    except Exception:
        await q.message.reply_text(
            "Напиши одно сообщение — отправлю как комментарий.",
            reply_markup=kb,
        )


# ── per-result rating ──────────────────────────────────────────────────────
@whitelist_only
async def rating_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """fb:rate:<task_uid>:<workflow>:<up|down>."""
    q = update.callback_query
    parts = q.data.split(":", 4)
    # ['fb', 'rate', task_uid, workflow, value]
    task_uid = parts[2]
    workflow = parts[3]
    value = parts[4]
    uid = update.effective_user.id
    if value == "up":
        await get_feedback_store().record_rating(
            uid=uid, task_uid=task_uid, workflow=workflow, value="up",
        )
        await q.answer("Спасибо за оценку.")
        return
    if value == "down":
        # Показываем экран причин в отдельном сообщении (не трогаем картинку).
        ctx.user_data["fb_reasons"] = {"task_uid": task_uid, "workflow": workflow, "selected": set()}
        await q.answer()
        await q.message.reply_text(
            "Что не так? Можно выбрать несколько причин.",
            reply_markup=_reasons_kb(task_uid, workflow, set()),
        )
        return


@whitelist_only
async def reasons_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """fb:rsn:<task_uid>:<workflow>:<action>[:<value>] — multi-toggle причин + завершение."""
    q = update.callback_query
    parts = q.data.split(":", 5)
    # ['fb', 'rsn', task_uid, workflow, action, value?]
    task_uid = parts[2]
    workflow = parts[3]
    action = parts[4]
    rstate = ctx.user_data.get("fb_reasons")
    if not rstate or rstate.get("task_uid") != task_uid:
        await q.answer("Сессия оценки устарела. Нажми 👎 ещё раз под картинкой.")
        return

    if action == "toggle":
        value = parts[5]
        sel: set[str] = rstate["selected"]
        if value in sel:
            sel.remove(value)
        else:
            sel.add(value)
        await q.answer()
        try:
            await q.edit_message_reply_markup(_reasons_kb(task_uid, workflow, sel))
        except Exception:
            pass
        return

    if action in ("done", "skip"):
        reasons = list(rstate["selected"]) if action == "done" else []
        # Сохраняем сразу — комментарий опционально следующим шагом.
        await get_feedback_store().record_rating(
            uid=update.effective_user.id,
            task_uid=task_uid,
            workflow=workflow,
            value="down",
            reasons=reasons,
        )
        ctx.user_data["fb_reasons"] = {
            "task_uid": task_uid, "workflow": workflow, "selected": set(reasons), "recorded": True,
        }
        await q.answer("Записал.")
        try:
            await q.edit_message_text(
                "Спасибо. Хочешь добавить пару слов?",
                reply_markup=_post_reasons_kb(task_uid, workflow),
            )
        except Exception:
            pass
        return

    if action == "comment":
        ctx.user_data["fb_pending"] = {
            "kind": "rating_comment", "task_uid": task_uid, "workflow": workflow,
        }
        await q.answer()
        try:
            await q.edit_message_text("Напиши одно сообщение — добавлю к оценке.")
        except Exception:
            pass
        return

    if action == "nocomment":
        ctx.user_data.pop("fb_reasons", None)
        await q.answer("Спасибо.")
        try:
            await q.edit_message_text("Спасибо за обратную связь.")
        except Exception:
            pass
        return


# ── auto-prompt callbacks ──────────────────────────────────────────────────
@whitelist_only
async def auto_prompt_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """fb:auto:<take|later|never>."""
    q = update.callback_query
    action = q.data.split(":", 2)[2]
    uid = update.effective_user.id
    store = get_feedback_store()
    if action == "take":
        await q.answer()
        await _start_survey(update, ctx)
        return
    if action == "later":
        await store.mark_prompt_shown(uid)
        await q.answer("Хорошо, спрошу позже.")
        try:
            await q.edit_message_text("Хорошо, спрошу позже.")
        except Exception:
            pass
        return
    if action == "never":
        await store.mark_prompt_declined(uid)
        await q.answer("Больше не побеспокою.")
        try:
            await q.edit_message_text("Понял, больше не предложу опрос (до следующей версии).")
        except Exception:
            pass
        return


# ── глобальный listener свободного текста ──────────────────────────────────
async def feedback_text_listener(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Перехватывает текстовое сообщение, если в `ctx.user_data["fb_pending"]`
    висит ожидание (free-вопрос опроса / комментарий / комментарий к оценке).

    Регистрируется в group=1 ПОСЛЕ ConversationHandler'ов и pending_edit_listener,
    но они не trigger'ят, если у юзера нет своих pending. На активной conversation
    (например, GEN_PROMPT) text всё равно перехватит conversation в group=0 первой —
    это намеренный приоритет: незаконченный сценарий важнее опроса.
    """
    pending = ctx.user_data.get("fb_pending")
    if not pending or not update.message or not update.message.text:
        return
    text = _trim(update.message.text)
    kind = pending.get("kind")
    uid = update.effective_user.id

    if kind == "survey_free":
        qid = pending["qid"]
        surv = ctx.user_data.get("fb_survey")
        if not surv:
            ctx.user_data.pop("fb_pending", None)
            return
        surv["answers"][qid] = text
        ctx.user_data.pop("fb_pending", None)
        _advance_step(ctx)
        # Перерисовать следующий шаг.
        step = surv["step"]
        total = len(SURVEY_QUESTIONS)
        if step >= total:
            text_msg = (
                f"Опрос пройден: {len(surv['answers'])} ответов, "
                f"{len(surv['skipped'])} пропусков.\nОтправить?"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("Отправить", callback_data="fb:srv:submit"),
                InlineKeyboardButton("Отменить", callback_data="fb:srv:cancel"),
            ]])
            await update.message.reply_text(text_msg, reply_markup=kb)
        else:
            qst = SURVEY_QUESTIONS[step]
            selected = surv.get("pending_multi", set()) if qst.kind == "multi" else set()
            qtext, kb = _render_question(qst, step, total, selected)
            if qst.kind == "free":
                ctx.user_data["fb_pending"] = {"kind": "survey_free", "qid": qst.id}
            await update.message.reply_text(qtext, reply_markup=kb, parse_mode="HTML")
        return

    if kind == "comment":
        category = pending["category"]
        await get_feedback_store().record_comment(uid=uid, category=category, text=text)
        ctx.user_data.pop("fb_pending", None)
        await update.message.reply_text("Спасибо, комментарий сохранён.")
        _ulog(update, "feedback:comment").info(f"category={category} len={len(text)}")
        return

    if kind == "rating_comment":
        task_uid = pending["task_uid"]
        workflow = pending["workflow"]
        # Дополняем уже записанную оценку: пишем отдельным событием rating с
        # тем же task_uid и комментарием — агрегация по task_uid склеит их в анализе.
        await get_feedback_store().record_rating(
            uid=uid, task_uid=task_uid, workflow=workflow,
            value="down", comment=text,
        )
        ctx.user_data.pop("fb_pending", None)
        ctx.user_data.pop("fb_reasons", None)
        await update.message.reply_text("Спасибо, добавил комментарий к оценке.")
        return


# ── хук успешной генерации (вызывается из scenarios._execute_task) ─────────
async def on_generation_success(uid: int, send_message_fn) -> None:
    """Инкремент счётчика; если подошло время — отправляем баннер опроса.

    `send_message_fn` — async callable, который умеет послать сообщение в чат
    юзера. Передаётся из scenarios.py чтобы избежать прямого импорта Bot/Chat.
    """
    store = get_feedback_store()
    await store.mark_generation_success(uid)
    if not store.should_auto_prompt(uid):
        return
    await store.mark_prompt_shown(uid)
    try:
        await send_message_fn(
            "Есть 3 минуты на короткий опрос? Поможет понять, что улучшить.",
            reply_markup=auto_prompt_kb(),
        )
    except Exception as e:
        logger.warning(f"auto-prompt send failed for uid={uid}: {type(e).__name__}: {e}")


# ── вспомогательное логирование ────────────────────────────────────────────
def _ulog(update: Update, action: str):
    u = update.effective_user
    return logger.bind(
        uid=u.id if u else 0,
        uname=(u.username or u.full_name or "?") if u else "?",
        action=action,
    )


# ── фабрика handler'ов для регистрации в Application ──────────────────────
def build_feedback_handlers() -> list:
    """Возвращает список (bucket, handler) — формат, который ждёт build_extra_handlers."""
    from telegram.ext import CallbackQueryHandler, MessageHandler, filters
    return [
        ("group0", CallbackQueryHandler(feedback_menu_cb, pattern=r"^menu:feedback$")),
        ("group0", CallbackQueryHandler(feedback_action_cb, pattern=r"^fb:menu:")),
        ("group0", CallbackQueryHandler(survey_cb, pattern=r"^fb:srv:")),
        ("group0", CallbackQueryHandler(comment_category_cb, pattern=r"^fb:cmt:")),
        ("group0", CallbackQueryHandler(rating_cb, pattern=r"^fb:rate:")),
        ("group0", CallbackQueryHandler(reasons_cb, pattern=r"^fb:rsn:")),
        ("group0", CallbackQueryHandler(auto_prompt_cb, pattern=r"^fb:auto:")),
        ("group1", MessageHandler(filters.TEXT & ~filters.COMMAND, feedback_text_listener)),
    ]
