"""Базовый класс воркфлоу. Конкретный transport (REST / WS / SSE) — после recon."""
from __future__ import annotations

import abc
from typing import Any, Awaitable, Callable

from loguru import logger

from client.api import PhygitalClient
from client.models import GenerationJob

ProgressCallback = Callable[[float], Awaitable[None]]


def _normalize_progress(raw: Any) -> float | None:
    """Приводим progress из Phygital к диапазону 0..1.

    HAR-recon показал что разные ноды возвращают по-разному: одни 0..100 (int),
    другие 0..1 (float). Эвристика: если value > 1.0 — это проценты, делим на 100.
    None / нечисловое / отрицательное — отбрасываем (callback не вызываем).
    """
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v < 0:
        return None
    if v > 1.0:
        v = v / 100.0
    # clamp на случай дрейфа Phygital (видели 101 в логах при completion)
    return max(0.0, min(1.0, v))


class Workflow(abc.ABC):
    """Базовый интерфейс: подготовить payload → submit → дождаться результата."""

    workflow_id: str = ""
    # Эвристика для synthetic progress: сколько в среднем длится этот воркфлоу
    # в running-фазе. Реальный progress из API (если есть) всегда побеждает —
    # это только fallback когда Phygital ничего не отдаёт. Подклассы могут
    # переопределять (image ~45s, photoroom ~15s, brand ~90s).
    EXPECTED_DURATION_S: float = 60.0
    SYNTH_PROGRESS_CAP: float = 0.95  # не дорисовываем до 100% — это сделает completion

    def __init__(self, client: PhygitalClient) -> None:
        self.client = client
        # _execute_task устанавливает callback перед .run() — wait() пушит сюда
        # нормализованный progress 0..1 при каждом изменении.
        self.on_progress: ProgressCallback | None = None

    @abc.abstractmethod
    def build_payload(self, **inputs: Any) -> dict[str, Any]: ...

    @abc.abstractmethod
    async def submit(self, payload: dict[str, Any]) -> str: ...  # → job_id

    @abc.abstractmethod
    async def wait(self, job_id: str, timeout: float = 300.0) -> GenerationJob: ...

    async def run(self, **inputs: Any) -> GenerationJob:
        payload = self.build_payload(**inputs)
        job_id = await self.submit(payload)
        return await self.wait(job_id)

    async def _emit_progress(self, raw: Any, last: float | None) -> float | None:
        """Вызывает on_progress если value валиден и отличается от last.
        Возвращает новое last (либо прежнее, если без изменений / без callback).
        Ошибка в callback не валит wait-loop — логируем и продолжаем.
        """
        value = _normalize_progress(raw)
        return await self._push_progress(value, last)

    async def _push_progress(self, value: float | None, last: float | None) -> float | None:
        """Низкоуровневый push: значение уже посчитано (real или synth).
        Дедуплицируем (с шагом 0.005 — иначе synth дёргает callback каждый poll).
        """
        if value is None:
            return last
        # Шаг 0.5% — иначе synth-режим бомбит Telegram edit_text каждый poll.
        if last is not None and abs(value - last) < 0.005:
            return last
        if self.on_progress is not None:
            try:
                await self.on_progress(value)
            except Exception as e:
                logger.debug(f"on_progress callback failed (suppressed): {e!r}")
        return value

    def _synth_progress(
        self,
        running_started_at: float | None,
        now: float,
    ) -> float | None:
        """Синтетический progress по elapsed-time. Возвращает None если
        running ещё не начинался — иначе linear ramp до SYNTH_PROGRESS_CAP.
        Используется только когда Phygital не отдаёт реальный progress.
        """
        if running_started_at is None:
            return None
        elapsed = max(0.0, now - running_started_at)
        return min(elapsed / max(0.001, self.EXPECTED_DURATION_S), self.SYNTH_PROGRESS_CAP)
