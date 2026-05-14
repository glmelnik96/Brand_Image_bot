"""Базовый класс воркфлоу. Конкретный transport (REST / WS / SSE) — после recon."""
from __future__ import annotations

import abc
from typing import Any

from client.api import PhygitalClient
from client.models import GenerationJob


class Workflow(abc.ABC):
    """Базовый интерфейс: подготовить payload → submit → дождаться результата."""

    workflow_id: str = ""

    def __init__(self, client: PhygitalClient) -> None:
        self.client = client

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
