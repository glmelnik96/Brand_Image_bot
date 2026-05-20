"""
Image generation workflow для Phygital+ "Nano Banana" (Gemini Image API).

Workflow id = 94, node global id = "Phygital Creator/phygc-rnd-gemini-image-api"

Полный flow (по recon):
  1. POST /api/v2/tasks/                 → {task_id}
  2. POST /api/v2/tasks/config_history   → null (но БЕЗ него таск висит в status='pending')
  3. GET  /api/v2/tasks/queue-position/  → polling до status='done'
  4. POST /api/v2/storage-object/.../download-links → S3 URLs
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from loguru import logger

from client.api import PhygitalClient
from client.models import GenerationJob
from workflows.base import Workflow

NODE_GLOBAL_ID = "Phygital Creator/phygc-rnd-gemini-image-api"
NODE_NAME = "Nano Banana"
SERVICE_VERSION = "0.0.42"
WORKFLOW_SCHEMA_ID = 94

# Промежуточные статусы — продолжаем поллить
PENDING_STATUSES = {"new", "pending", "running", "queued", "in_progress", "waiting_for_launch"}
DONE_STATUSES = {"done", "completed", "success"}
# error_params — терминальный safety/validation reject Phygital'a (position=-1, error_message
# содержит человекочитаемую причину типа «Please remove potential harmful word ...»). Без него
# полинг до timeout (600s/180s) держит global_sem и не показывает юзеру причину.
FAIL_STATUSES = {"failed", "error", "canceled", "cancelled", "error_params"}


class ImageGenWorkflow(Workflow):
    """Универсальная генерация изображения по prompt.

    Параметры по умолчанию совпадают с теми, что использовал фронт в recon:
    model_name="v3", ratio="default", resolution="k2".
    """

    workflow_id = str(WORKFLOW_SCHEMA_ID)

    def __init__(
        self,
        client: PhygitalClient,
        *,
        model_name: str = "v3",
        ratio: str = "default",
        resolution: str = "k2",
    ) -> None:
        super().__init__(client)
        self.model_name = model_name
        self.ratio = ratio
        self.resolution = resolution
        self._last_prompt: str = ""
        self._last_price: dict[str, Any] | None = None

    # ── Payload для POST /api/v2/tasks/ ───────────────────────────────────
    def build_payload(self, *, prompt: str, init_img: list[Any] | None = None) -> dict[str, Any]:
        self._last_prompt = prompt
        return {
            "id": WORKFLOW_SCHEMA_ID,
            "inputs": [
                {"name": "text_prompt", "type": "text", "optional": None,
                 "isModified": True, "value": prompt, "meta": {}},
                {"name": "init_img", "type": "array", "optional": None,
                 "isModified": bool(init_img), "value": init_img or [],
                 "meta": {"dimensions": []}},
            ],
            "params": self._params_list(),
            "outputs": [{"name": "image", "type": "array", "value": ""}],
        }

    def _params_list(self) -> list[dict[str, Any]]:
        return [
            {"name": "model_name", "type": "enum", "value": self.model_name, "meta": {}},
            {"name": "ratio", "type": "enum", "value": self.ratio, "meta": {}},
            {"name": "resolution", "type": "enum", "value": self.resolution, "meta": {}},
        ]

    # ── Payload для config_history (полный node-graph) ────────────────────
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
            },
            "outputSocketGroup": [
                {
                    "name": "image",
                    "dataType": "array",
                    "optionalInfo": {"valueOptions": {"itemType": {"dataType": "image"}}},
                    "optional": None,
                    "displayName": None,
                    "value": [],
                }
            ],
            "meta": {
                "text_prompttextSelector": {"highlights": []},
                **({"taskPrice": self._last_price} if self._last_price else {}),
                "taskSchema": {
                    "id": WORKFLOW_SCHEMA_ID,
                    "inputs": [
                        {"name": "text_prompt", "type": "text", "optional": None,
                         "isModified": True, "value": prompt, "meta": {}},
                        {"name": "init_img", "type": "array", "optional": None,
                         "isModified": False, "value": [], "meta": {"dimensions": []}},
                    ],
                    "params": self._params_list(),
                    "outputs": [{"name": "image", "type": "array", "value": ""}],
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
        # 1. опционально: спросить цену (это влияет только на meta.taskPrice в config_history)
        try:
            price_payload = {
                "id": WORKFLOW_SCHEMA_ID,
                "inputs": [{"name": "init_img", "value": None, "type": "image", "meta": {}}],
                "params": self._params_list(),
                "outputs": [],
            }
            self._last_price = await self.client.get_credits_price(price_payload)
            logger.debug(f"price: {self._last_price.get('price')}")
        except Exception as e:
            logger.warning(f"price lookup failed (non-fatal): {e}")

        # 2. submit
        task_id = await self.client.submit_task(payload)
        logger.info(f"Submitted task_id={task_id}")

        # 3. config_history — обязательный шаг, иначе таск висит в pending
        config = self._build_config(self._last_prompt)
        await self.client.post_config_history(task_id, config)
        logger.info(f"Posted config_history for task {task_id}")

        return str(task_id)

    async def wait(
        self,
        job_id: str,
        timeout: float = 600.0,
        poll_interval: float = 1.5,
    ) -> GenerationJob:
        task_id = int(job_id)
        deadline = asyncio.get_event_loop().time() + timeout
        last_status: str | None = None

        while asyncio.get_event_loop().time() < deadline:
            data = await self.client.task_status(task_id)
            status = (data.get("status") or "").lower()
            if status != last_status:
                logger.info(f"task {task_id}: {status} (position={data.get('position')}, progress={data.get('progress')})")
                last_status = status

            if status in DONE_STATUSES:
                link_ids = self._extract_link_ids(data.get("outputs") or [])
                if not link_ids:
                    return GenerationJob(
                        job_id=job_id, status="failed",
                        error="task done but no output link_ids", raw=data,
                    )
                links = await self.client.get_download_links(link_ids)
                urls = [lnk["download_link"] for lnk in links if lnk.get("download_link")]
                return GenerationJob(
                    job_id=job_id, status="completed",
                    result_urls=urls, raw={"task": data, "links": links},
                )

            if status in FAIL_STATUSES:
                return GenerationJob(
                    job_id=job_id, status="failed",
                    error=data.get("error_message") or f"status={status}", raw=data,
                )

            if status and status not in PENDING_STATUSES:
                logger.warning(f"Unknown status '{status}', treating as pending")

            await asyncio.sleep(poll_interval)

        return GenerationJob(job_id=job_id, status="failed", error="timeout")

    @staticmethod
    def _extract_link_ids(outputs: list[dict[str, Any]]) -> list[int]:
        """outputs: [{name:'image', type:'array', value:'', id:[14996399]}]"""
        ids: list[int] = []
        for out in outputs:
            raw = out.get("id")
            if isinstance(raw, list):
                ids.extend(int(x) for x in raw)
            elif isinstance(raw, int):
                ids.append(raw)
        return ids
