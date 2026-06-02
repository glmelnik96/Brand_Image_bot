"""
Photoroom background removal (Phygital workflow id=125).

NODE_GLOBAL_ID = "Phygital Creator/phygc-rnd-photoroom-api"

Recon HAR 2026-06-02 (entry 765/769 → POST /api/v2/tasks/, task_id=7325845):
  POST /api/v2/tasks/  →  {"task_id": <int>}
  body:
    id: 125
    inputs: [{ name="init_img", type="image", value=<file_obj_id>,
               meta.dimensions={height,width} }]
    params:
      - bg_color (text)   = null     # null → прозрачный PNG; HEX → залить цветом
      - size    (enum)    = "full"
      - crop    (bool)    = false
      - despill (bool)    = true     # подавление цвета фона, попадающего в волосы/края
    outputs: [{ name="out_image", type="image", value="" }]

  Затем тот же config_history-шаг что у остальных нод (без него таск висит в pending).
  Polling — стандартный через task_status.

Возвращает один PNG с прозрачностью (alpha-channel сохраняется S3-апстримом).

Использование (см. workflows/speaker_prep.py и scenarios.speaker_bg_cb):
    wf = PhotoroomBgRemoveWorkflow(client)
    job = await wf.run_with_file(init_path=<Path>)
    # job.result_urls = [https://...png] (один URL)
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from client.api import PhygitalClient
from client.models import GenerationJob
from workflows.base import Workflow
from workflows.image_gen import (
    DONE_STATUSES,
    FAIL_STATUSES,
    PENDING_STATUSES,
    ImageGenWorkflow,
)
from workflows.image_to_image import _prepare_for_upload

NODE_GLOBAL_ID = "Phygital Creator/phygc-rnd-photoroom-api"
NODE_NAME = "Photoroom"
SERVICE_VERSION = "0.0.68"
WORKFLOW_SCHEMA_ID = 125


class PhotoroomBgRemoveWorkflow(Workflow):
    """Удаление фона через Photoroom-ноду. Один init_img → один out_image (PNG, alpha)."""

    workflow_id = str(WORKFLOW_SCHEMA_ID)
    # Photoroom — быстрая нода, ~5–15s по UI.
    EXPECTED_DURATION_S = 15.0

    def __init__(
        self,
        client: PhygitalClient,
        *,
        bg_color: str | None = None,  # None → прозрачный
        size: str = "full",
        crop: bool = False,
        despill: bool = True,
    ) -> None:
        super().__init__(client)
        self.bg_color = bg_color
        self.size = size
        self.crop = crop
        self.despill = despill
        # Заполняется в run_with_file через upload.
        self._init_img_id: int = 0
        self._init_img_dim: dict[str, int] = {}
        self._last_price: dict[str, Any] | None = None

    # ── payload ──────────────────────────────────────────────────────────
    def _params_list(self) -> list[dict[str, Any]]:
        return [
            {"name": "bg_color", "type": "text", "value": self.bg_color, "meta": {}},
            {"name": "size", "type": "enum", "value": self.size, "meta": {}},
            {"name": "crop", "type": "bool", "value": self.crop, "meta": {}},
            {"name": "despill", "type": "bool", "value": self.despill, "meta": {}},
        ]

    def build_payload(self, **_inputs: Any) -> dict[str, Any]:
        # _inputs игнорируются: значения берутся из upload-шага (self._init_img_id/dim).
        return {
            "id": WORKFLOW_SCHEMA_ID,
            "inputs": [
                {
                    "name": "init_img",
                    "type": "image",
                    "optional": None,
                    "isModified": False,
                    "value": self._init_img_id,
                    "meta": {"dimensions": dict(self._init_img_dim)},
                },
            ],
            "params": self._params_list(),
            "outputs": [{"name": "out_image", "type": "image", "value": ""}],
        }

    def _build_config(self) -> dict[str, Any]:
        node_uuid = str(uuid.uuid4())
        node = {
            "globalId": NODE_GLOBAL_ID,
            "name": NODE_NAME,
            "uuid": node_uuid,
            "taskID": 0,
            "serviceVersion": SERVICE_VERSION,
            "inputSocketGroup": {
                "init_img": {
                    "name": "init_img",
                    "type": "image",
                    "value": self._init_img_id or None,
                    "optionalInfo": {
                        "isEnabled": True,
                        "mapOfEnabylity": {},
                        "originalWorkspaceIds": self._init_img_id or None,
                    },
                },
            },
            "outputSocketGroup": [
                {
                    "name": "out_image",
                    "dataType": "image",
                    "optionalInfo": {},
                    "optional": None,
                    "displayName": None,
                    "value": "",
                }
            ],
            "meta": {
                **({"taskPrice": self._last_price} if self._last_price else {}),
                "taskSchema": {
                    "id": WORKFLOW_SCHEMA_ID,
                    "inputs": [
                        {
                            "name": "init_img",
                            "type": "image",
                            "optional": None,
                            "isModified": False,
                            "value": self._init_img_id,
                            "meta": {"dimensions": dict(self._init_img_dim)},
                        },
                    ],
                    "params": self._params_list(),
                    "outputs": [{"name": "out_image", "type": "image", "value": ""}],
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
            "height": 400,
        }
        return {"nodes": [node], "executedNodeUuid": node_uuid}

    # ── API calls ────────────────────────────────────────────────────────
    async def submit(self, payload: dict[str, Any]) -> str:
        # price — best-effort
        try:
            price_payload = {
                "id": WORKFLOW_SCHEMA_ID,
                "inputs": [{"name": "init_img", "value": None, "type": "image", "meta": {}}],
                "params": self._params_list(),
                "outputs": [],
            }
            self._last_price = await self.client.get_credits_price(price_payload)
            logger.debug(f"photoroom price: {self._last_price.get('price')}")
        except Exception as e:
            logger.warning(f"photoroom price lookup failed (non-fatal): {e}")

        task_id = await self.client.submit_task(payload)
        logger.info(f"[photoroom] Submitted task_id={task_id}")

        config = self._build_config()
        await self.client.post_config_history(task_id, config)
        logger.info(f"[photoroom] Posted config_history for task {task_id}")
        return str(task_id)

    async def wait(
        self,
        job_id: str,
        timeout: float = 180.0,
        poll_interval: float = 1.5,
    ) -> GenerationJob:
        # Photoroom — быстрая нода (~5-15s по UI), 180s более чем достаточно.
        task_id = int(job_id)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        last_status: str | None = None
        last_progress: float | None = None
        running_started_at: float | None = None

        while loop.time() < deadline:
            data = await self.client.task_status(task_id)
            status = (data.get("status") or "").lower()
            if status != last_status:
                logger.info(
                    f"[photoroom] task {task_id}: {status} "
                    f"(position={data.get('position')}, progress={data.get('progress')})"
                )
                last_status = status

            if status in ("running", "in_progress"):
                if running_started_at is None:
                    running_started_at = loop.time()
                raw = data.get("progress")
                if raw is not None:
                    last_progress = await self._emit_progress(raw, last_progress)
                else:
                    synth = self._synth_progress(running_started_at, loop.time())
                    last_progress = await self._push_progress(synth, last_progress)

            if status in DONE_STATUSES:
                link_ids = ImageGenWorkflow._extract_link_ids(data.get("outputs") or [])
                if not link_ids:
                    return GenerationJob(
                        job_id=job_id, status="failed",
                        error="photoroom done but no output link_ids", raw=data,
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
                logger.warning(f"[photoroom] Unknown status '{status}', treating as pending")

            await asyncio.sleep(poll_interval)

        return GenerationJob(job_id=job_id, status="failed", error="timeout")

    # ── high-level entrypoint ────────────────────────────────────────────
    async def run_with_file(self, *, init_path: str | Path) -> GenerationJob:
        """Аплоадит one init-картинку и запускает Photoroom-удаление фона."""
        p = Path(init_path).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(p)
        effective, dim, normalized = _prepare_for_upload(p)
        try:
            self._init_img_id = await self.client.upload_file(effective)
        finally:
            if normalized and effective != p:
                try:
                    effective.unlink()
                except OSError:
                    pass
        self._init_img_dim = dim
        logger.info(
            f"[photoroom] uploaded {p.name} → file_obj_id={self._init_img_id} "
            f"({dim['width']}x{dim['height']})"
        )
        return await self.run()
