"""Pydantic-модели запросов/ответов. Заполняются после recon."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class GenerationRequest(BaseModel):
    """Универсальный запрос на генерацию. Конкретные поля — после recon."""
    prompt: str
    workflow_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class GenerationJob(BaseModel):
    """Состояние задачи."""
    job_id: str
    status: Literal["pending", "running", "completed", "failed"] = "pending"
    result_urls: list[str] = Field(default_factory=list)
    result_text: str | None = None  # для нод с текстовым output (например, Gemini Text)
    error: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
