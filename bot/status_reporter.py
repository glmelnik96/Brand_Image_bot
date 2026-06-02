"""Status reporter для UI задач бота.

Отвечает за единственное status-сообщение в чате, которое:
  - редактируется при смене этапа (`set_step`) — Gemini Text → Nano Banana → scrub → ...
  - тикает live elapsed-time в фоне (1× / 3 сек) пока задача в работе,
  - помнит длительность каждого этапа и показывает её в финальном сообщении.

Формат строки:

    {label} — {phase}
    {sub} • {elapsed} / {eta}

где `phase` — общее состояние («принято», «в очереди», «генерация», «готово», «ошибка»),
а `sub` — текущий этап («Gemini Text», «Nano Banana», «Чистка safety-words «knob»» …).

Дизайн без декоративных эмодзи (см. memory feedback_ui_minimal_emoji): разделители —
тире (—), bullet (•), новая строка. Никаких ✅/⏱/🎨.

Telegram rate-limit на edit_message_text — 1–2 раза/сек безопасно. Тик 3s + edit при
смене этапа даёт ≤ ~1 edit/сек в среднем.
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from loguru import logger
from telegram import Message


def _fmt_mmss(seconds: float) -> str:
    """`0:23`, `1:42`, `12:05`. Always M:SS."""
    s = max(0, int(seconds))
    m, sec = divmod(s, 60)
    return f"{m}:{sec:02d}"


def _fmt_eta(seconds: int | None) -> str:
    """`~1:30`. Для None — пустая строка."""
    if not seconds:
        return ""
    return f"~{_fmt_mmss(seconds)}"


class StatusReporter:
    """Один экземпляр на одну задачу. Жизненный цикл:

        reporter = StatusReporter(status, label, eta_sec=90)
        await reporter.queued(queue_pos=2)            # сразу после enqueue
        ...                                            # ожидание global_sem
        await reporter.waiting_slot()                  # вошли в _execute_task
        await reporter.start("Gemini Text")            # acquire global_sem, погнали
        ...
        await reporter.step("Nano Banana")             # этап сменился (workflow прислал progress)
        ...
        await reporter.done(job_id="123")              # успех
        # ИЛИ
        await reporter.error("Please remove harmful word knob…")
        # ИЛИ
        await reporter.crashed("RuntimeError: …")

    Все методы безопасно глотают exceptions из Telegram API (rate-limit / message_not_modified
    и т.п.) — статус-сообщение не должно ронять задачу.
    """

    # Период обновления elapsed-time в фоне. 3 секунды — компромисс между «отзывчиво»
    # и «не флудим Telegram». Можно уменьшить, если будем уверены в rate-limit.
    TICK_SECONDS = 3.0

    def __init__(self, status: Message, label: str, eta_sec: int | None) -> None:
        self.status = status
        self.label = label
        self.eta_sec = eta_sec
        self._eta_str = _fmt_eta(eta_sec)

        # Состояние
        self._phase: str = "принято"        # принято / в очереди / жду слот / генерация / готово / ошибка
        self._sub: str = ""                  # текущий этап (Gemini Text / Nano Banana / ...)
        self._extra_line: str = ""           # дополнительная строка под основным статусом (для ошибок)
        # Прогресс текущего этапа в 0..1. Сбрасывается при step() / start().
        # Если задан — в _compose_text используется вместо elapsed/eta хвоста.
        self._progress: float | None = None

        # Тайминги
        self._run_started: float | None = None        # когда поехала генерация (после global_sem)
        self._step_started: float | None = None
        self._step_times: list[tuple[str, float]] = []  # [(step_name, seconds), ...]

        # Background ticker
        self._tick_task: asyncio.Task | None = None
        self._last_rendered: str = ""

    # ── lifecycle ────────────────────────────────────────────────────────

    async def queued(self, *, queue_pos: int) -> None:
        """Сразу после enqueue: «Принято. Очередь №N»."""
        self._phase = "принято"
        self._sub = f"очередь №{queue_pos}"
        await self._render()

    async def waiting_slot(self) -> None:
        """Вошли в _execute_task, ждём global_sem. Запускаем live-ticker без gen_started."""
        self._phase = "в очереди"
        self._sub = "жду свободный слот"
        # Тикер с самого входа в _execute_task — пусть юзер видит ожидание.
        if self._run_started is None:
            self._run_started = time.monotonic()
        self._ensure_tick_running()
        await self._render()

    async def start(self, first_step: str) -> None:
        """Acquired global_sem, начинаем генерацию. first_step — название первого этапа."""
        now = time.monotonic()
        # Если ticker уже шёл (queued/waiting_slot тикали), не сбрасываем — пусть продолжает с того же 0.
        if self._run_started is None:
            self._run_started = now
        self._phase = "генерация"
        self._sub = first_step
        self._step_started = now
        self._progress = None
        self._ensure_tick_running()
        await self._render()

    async def step(self, name: str) -> None:
        """Workflow прислал транзишн этапа. Записываем длительность предыдущего и переключаемся."""
        now = time.monotonic()
        if self._step_started is not None and self._sub:
            dur = now - self._step_started
            self._step_times.append((self._sub, dur))
        self._sub = name
        self._step_started = now
        # Новый этап — сбрасываем прогресс. Workflow начнёт пушить с 0..1 заново.
        self._progress = None
        await self._render()

    async def progress(self, value: float) -> None:
        """Колбэк для Workflow.on_progress (0..1). Обновляет процент в текущем этапе.
        Сам процент рендерит _compose_text — здесь только сохраняем + триггерим render.
        """
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        v = max(0.0, min(1.0, v))
        # Дедуп шагом 1% — иначе при synth-режиме edit_text спамится каждый poll.
        if self._progress is not None and abs(v - self._progress) < 0.01:
            return
        self._progress = v
        await self._render()

    async def done(self, *, job_id: str | None) -> None:
        await self._stop_tick()
        now = time.monotonic()
        # Дозаписываем длительность последнего этапа
        if self._step_started is not None and self._sub:
            self._step_times.append((self._sub, now - self._step_started))
        total = (now - self._run_started) if self._run_started is not None else 0.0
        self._phase = "готово"
        breakdown = self._breakdown_str()
        sub = f"за {_fmt_mmss(total)}"
        if breakdown:
            sub = f"{sub} • {breakdown}"
        if job_id:
            sub = f"{sub} • job {job_id}"
        self._sub = sub
        self._extra_line = ""
        await self._render(force=True)

    async def error(self, message: str) -> None:
        """Финальная ошибка от workflow (job.status != completed)."""
        await self._stop_tick()
        now = time.monotonic()
        if self._step_started is not None and self._sub:
            self._step_times.append((self._sub, now - self._step_started))
        total = (now - self._run_started) if self._run_started is not None else 0.0
        last_step = self._sub
        self._phase = "ошибка"
        sub = f"через {_fmt_mmss(total)}"
        if last_step:
            sub = f"{sub} • {last_step}"
        self._sub = sub
        self._extra_line = (message or "").strip()
        await self._render(force=True)

    async def crashed(self, message: str) -> None:
        """Эксепшен в раннере. Поведение как у error, но фраза другая."""
        await self.error(message)

    # ── internals ────────────────────────────────────────────────────────

    def _ensure_tick_running(self) -> None:
        if self._tick_task is not None and not self._tick_task.done():
            return
        loop = asyncio.get_event_loop()
        self._tick_task = loop.create_task(self._tick_loop())

    async def _stop_tick(self) -> None:
        if self._tick_task is None:
            return
        self._tick_task.cancel()
        try:
            await self._tick_task
        except (asyncio.CancelledError, Exception):
            pass
        self._tick_task = None

    async def _tick_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.TICK_SECONDS)
                await self._render()
        except asyncio.CancelledError:
            return
        except Exception as e:  # pragma: no cover
            logger.debug(f"[status] tick loop crashed (non-fatal): {e!r}")

    def _breakdown_str(self) -> str:
        """`Gemini Text 0:23 • Nano Banana 1:19`. Пустая строка если шагов <2."""
        if len(self._step_times) < 2:
            return ""
        return " • ".join(f"{name} {_fmt_mmss(dur)}" for name, dur in self._step_times)

    def _compose_text(self) -> str:
        """Финальная строка для edit_text."""
        line1 = f"{self.label} — {self._phase}"
        # elapsed[/eta] — только если генерация уже стартовала.
        # В фазе «генерация» если воркфлоу пушит progress — показываем процент
        # вместо eta (по аналогии с панелью Phygital-Adobe-Studio).
        if self._run_started is not None and self._phase in {"в очереди", "генерация"}:
            elapsed = time.monotonic() - self._run_started
            elapsed_str = _fmt_mmss(elapsed)
            if self._phase == "генерация" and self._progress is not None:
                pct = int(round(self._progress * 100))
                tail = f"{elapsed_str} • {pct}%"
            elif self._eta_str:
                tail = f"{elapsed_str} / {self._eta_str}"
            else:
                tail = elapsed_str
            sub_with_time = f"{self._sub} • {tail}" if self._sub else tail
        else:
            sub_with_time = self._sub
        lines = [line1]
        if sub_with_time:
            lines.append(sub_with_time)
        if self._extra_line:
            lines.append(self._extra_line)
        return "\n".join(lines)

    async def _render(self, *, force: bool = False) -> None:
        text = self._compose_text()
        if not force and text == self._last_rendered:
            return
        try:
            await self.status.edit_text(text)
            self._last_rendered = text
        except Exception as e:
            # Чаще всего — "Message is not modified" (одинаковый текст) или rate-limit. Логируем debug.
            logger.debug(f"[status] edit_text failed (non-fatal): {e!r}")


# Удобный alias для типа коллбэка, который workflow передаёт обратно — теперь это
# StatusReporter.step (или совместимая корутина, принимающая str имя этапа).
ProgressCb = Callable[[str], Awaitable[None]]
