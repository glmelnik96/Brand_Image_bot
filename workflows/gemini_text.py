"""
Gemini Text workflow (Phygital+ node id=72, "Gemini text").

NODE_GLOBAL_ID = "Phygital Creator/phygc-rnd-gemini-text-api"
Inputs:
  - text_prompt (text)
  - init_img (array of image, max 900) — опционально
  - videos (array)                       — пока не используем
  - audio                                — пока не используем
  - documents (array of pdf/txt/md/csv/...) — system_prompt-документы
Outputs:
  - description (text)

Flow тот же, что у image_gen: submit task → config_history → polling → достать
result_text из outputs. По схеме каталога description = text → ожидаем,
что значение лежит прямо в outputs[].value (не через storage-download).
Если форма ответа окажется другой — `_extract_description` логирует raw
и возвращает пустую строку, чтобы можно было быстро откорректировать.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from loguru import logger

from client.api import PhygitalClient
from client.models import GenerationJob
from workflows.base import Workflow
from workflows.image_gen import DONE_STATUSES, FAIL_STATUSES, PENDING_STATUSES

NODE_GLOBAL_ID = "Phygital Creator/phygc-rnd-gemini-text-api"
NODE_NAME = "Gemini text"
SERVICE_VERSION = "0.0.23"
WORKFLOW_SCHEMA_ID = 72


class GeminiTextWorkflow(Workflow):
    """Single-node Gemini Text. Возвращает description как result_text."""

    workflow_id = str(WORKFLOW_SCHEMA_ID)

    def __init__(
        self,
        client: PhygitalClient,
        *,
        model: str = "pro_3_1",
        thinking_level: str = "high",
    ) -> None:
        super().__init__(client)
        self.model = model
        self.thinking_level = thinking_level
        self._last_prompt: str = ""
        self._last_price: dict[str, Any] | None = None
        self._init_img_ids: list[int] = []
        self._init_img_dims: list[dict[str, int]] = []
        self._document_ids: list[int] = []

    # ── payload для POST /api/v2/tasks/ ───────────────────────────────────
    def build_payload(self, *, prompt: str) -> dict[str, Any]:
        self._last_prompt = prompt
        return {
            "id": WORKFLOW_SCHEMA_ID,
            "inputs": self._inputs_list(prompt, is_task_schema=False),
            "params": self._params_list(),
            "outputs": [{"name": "description", "type": "text", "value": ""}],
        }

    def _params_list(self) -> list[dict[str, Any]]:
        return [
            {"name": "model", "type": "enum", "value": self.model, "meta": {}},
            {"name": "thinking_level", "type": "enum", "value": self.thinking_level, "meta": {}},
        ]

    def _inputs_list(self, prompt: str, *, is_task_schema: bool) -> list[dict[str, Any]]:
        """Список inputs для tasks/ и meta.taskSchema.

        Шейп взят 1-в-1 из recon (UI POST /api/v2/tasks/ schema=72):
          - documents.type = "document" (НЕ "array" — backend иначе вешает таск в pending)
          - init_img.type = "image" при заполнении, иначе "array" (как в image_to_image)
          - audio.value = "" (не None)
          - isModified везде False, даже у заполненных полей
        """
        if self._init_img_ids:
            init_img = {"name": "init_img", "type": "image", "optional": None,
                        "isModified": False,
                        "value": list(self._init_img_ids),
                        "meta": {"dimensions": list(self._init_img_dims)}}
        else:
            init_img = {"name": "init_img", "type": "array", "optional": None,
                        "isModified": False, "value": [],
                        "meta": {"dimensions": []}}

        return [
            {"name": "text_prompt", "type": "text", "optional": None,
             "isModified": False, "value": prompt, "meta": {}},
            init_img,
            {"name": "videos", "type": "array", "optional": None,
             "isModified": False, "value": [], "meta": {}},
            {"name": "audio", "type": "audio", "optional": True,
             "isModified": False, "value": "", "meta": {}},
            {"name": "documents", "type": "document", "optional": None,
             "isModified": False, "value": list(self._document_ids), "meta": {}},
        ]

    # ── config_history (полный node-graph) ────────────────────────────────
    def _build_config(self, prompt: str) -> dict[str, Any]:
        node_uuid = str(uuid.uuid4())
        node = {
            "globalId": NODE_GLOBAL_ID,
            "name": NODE_NAME,
            "uuid": node_uuid,
            "taskID": 0,
            "serviceVersion": SERVICE_VERSION,
            "inputSocketGroup": {
                "text_prompt": {
                    "name": "text_prompt",
                    "type": "text",
                    "value": prompt,
                    "optionalInfo": {
                        "isEnabled": True,
                        "mapOfEnabylity": {},
                        "originalWorkspaceIds": prompt,
                    },
                },
                "init_img": {
                    "name": "init_img",
                    "type": "array",
                    "value": None,
                    "optionalInfo": {
                        "isEnabled": True,
                        "mapOfEnabylity": {},
                        "originalWorkspaceIds": None,
                    },
                },
                "videos": {
                    "name": "videos", "type": "array", "value": None,
                    "optionalInfo": {"isEnabled": True, "mapOfEnabylity": {},
                                     "originalWorkspaceIds": None},
                },
                "audio": {
                    "name": "audio", "type": "audio", "value": None,
                    "optionalInfo": {"isEnabled": True, "mapOfEnabylity": {},
                                     "originalWorkspaceIds": None},
                },
                "documents": {
                    "name": "documents", "type": "array", "value": None,
                    "optionalInfo": {"isEnabled": True, "mapOfEnabylity": {},
                                     "originalWorkspaceIds": None},
                },
            },
            "outputSocketGroup": [
                {
                    "name": "description",
                    "dataType": "text",
                    "optionalInfo": {},
                    "optional": None,
                    "displayName": None,
                    "value": "",
                }
            ],
            "meta": {
                "text_prompttextSelector": {"highlights": []},
                **({"taskPrice": self._last_price} if self._last_price else {}),
                "taskSchema": {
                    "id": WORKFLOW_SCHEMA_ID,
                    "inputs": self._inputs_list(prompt, is_task_schema=True),
                    "params": self._params_list(),
                    "outputs": [{"name": "description", "type": "text", "value": ""}],
                },
            },
            "params": {
                p["name"]: {
                    "name": p["name"],
                    "type": p["type"],
                    "optionalInfo": {"isEnabled": True, "mapOfEnabylity": {}},
                    "value": p["value"],
                }
                for p in self._params_list()
            },
            "width": 350,
            "position": {"x": 600, "y": 200},
            "connections": [],
            "height": 617,
        }
        return {"nodes": [node], "executedNodeUuid": node_uuid}

    # ── API calls ─────────────────────────────────────────────────────────
    async def submit(self, payload: dict[str, Any]) -> str:
        try:
            price_payload = {
                "id": WORKFLOW_SCHEMA_ID,
                "inputs": [{"name": "init_img", "value": None, "type": "image", "meta": {}}],
                "params": self._params_list(),
                "outputs": [],
            }
            self._last_price = await self.client.get_credits_price(price_payload)
            logger.debug(f"gemini-text price: {self._last_price.get('price')}")
        except Exception as e:
            logger.warning(f"gemini-text price lookup failed (non-fatal): {e}")

        task_id = await self.client.submit_task(payload)
        logger.info(f"[gemini-text] Submitted task_id={task_id}")

        config = self._build_config(self._last_prompt)
        await self.client.post_config_history(task_id, config)
        logger.info(f"[gemini-text] Posted config_history for task {task_id}")
        return str(task_id)

    async def wait(
        self,
        job_id: str,
        timeout: float = 180.0,
        poll_interval: float = 1.5,
    ) -> GenerationJob:
        # 180s ≈ 3× медианы Gemini Text (26–60s по логам). Залипший таск не должен
        # держать global_sem 5+ минут — лучше быстро failed и юзер ретраит.
        task_id = int(job_id)
        deadline = asyncio.get_event_loop().time() + timeout
        last_status: str | None = None

        while asyncio.get_event_loop().time() < deadline:
            data = await self.client.task_status(task_id)
            status = (data.get("status") or "").lower()
            if status != last_status:
                logger.info(
                    f"[gemini-text] task {task_id}: {status} "
                    f"(position={data.get('position')}, progress={data.get('progress')})"
                )
                last_status = status

            if status in DONE_STATUSES:
                text = self._extract_description(data.get("outputs") or [], raw=data)
                if not text:
                    return GenerationJob(
                        job_id=job_id, status="failed",
                        error="gemini-text done but description is empty", raw=data,
                    )
                return GenerationJob(
                    job_id=job_id, status="completed",
                    result_text=text, raw={"task": data},
                )

            if status in FAIL_STATUSES:
                return GenerationJob(
                    job_id=job_id, status="failed",
                    error=data.get("error_message") or f"status={status}", raw=data,
                )

            if status and status not in PENDING_STATUSES:
                logger.warning(f"[gemini-text] Unknown status '{status}', treating as pending")

            await asyncio.sleep(poll_interval)

        return GenerationJob(job_id=job_id, status="failed", error="timeout")

    @staticmethod
    def _extract_description(outputs: list[dict[str, Any]], *, raw: dict[str, Any]) -> str:
        """Достаём description-текст из outputs. Расширяемо — пробуем несколько форм,
        логируем raw если не нашли (для recon-fallback)."""
        for out in outputs:
            if out.get("name") != "description":
                continue
            # 1) value напрямую строкой
            v = out.get("value")
            if isinstance(v, str) and v.strip():
                return v
            # 2) value в виде {"text": "..."} или {"description": "..."}
            if isinstance(v, dict):
                for k in ("text", "description", "result", "content"):
                    if isinstance(v.get(k), str) and v[k].strip():
                        return v[k]
            # 3) поле text/result/content рядом с value
            for k in ("text", "result", "content"):
                if isinstance(out.get(k), str) and out[k].strip():
                    return out[k]
        logger.error(f"[gemini-text] couldn't extract description from outputs={outputs!r} raw={raw!r}")
        return ""

    # ── high-level helpers ────────────────────────────────────────────────
    async def run_text(
        self,
        *,
        prompt: str,
        init_img_ids: list[int] | None = None,
        init_img_dims: list[dict[str, int]] | None = None,
        document_ids: list[int] | None = None,
    ) -> GenerationJob:
        """Удобный entrypoint: задаём init_img_ids/document_ids явно, без аплоада.
        Аплоад делается на уровне композеров (brand_text2img / brand_img2img),
        чтобы переиспользовать file_obj_id между нодами."""
        self._init_img_ids = list(init_img_ids or [])
        self._init_img_dims = list(init_img_dims or [{} for _ in self._init_img_ids])
        self._document_ids = list(document_ids or [])
        return await self.run(prompt=prompt)
