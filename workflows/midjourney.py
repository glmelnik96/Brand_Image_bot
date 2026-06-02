"""
Midjourney-нода (Phygital workflow id=82 для Imagine, 85 для Upscale, 84 для Variation).

Recon HAR 2026-06-02 (`recon/captures/phygital-20260602-04*.har`):

  Imagine (id=82, NODE_GLOBAL_ID = phygc-rnd-midjourney-api-imagine):
    inputs: text_prompt + negative_prompt + init_img (img2img) + style/character/omni_reference
    params (все обязательны): aspect_ratio="square", model_version="v7",
      chaos=0, stylize=100, style_weight=100, omn_weight=100, char_weight=100,
      raw_mode=true, tile=false, weird=0, image_weight=1
    output: image_grid (array of 4 images) — MJ возвращает grid 2x2

  Upscale (id=85, phygc-rnd-midjourney-api-upscale):
    inputs: task_id (str) + image_num (str "1".."4")
    params: []
    output: image_grid (array)
    config_history.meta: {fromTaskID: <int>, fromImageIndex: <0..3>}

  Variation (id=84, phygc-rnd-midjourney-api-variation):
    inputs: task_id (str) + image_num (str "1".."4")
    params: []
    output: image_grid (array)
    config_history.meta: {fromTaskID: <int>, fromImageIndex: <0..3>}

  Polling — стандартный через task_status (status: running/done/error_params/etc).

Endpoint: app-server-azure.phygital.plus/api/v2/tasks/ (как и все Phygital-ноды).
"""

from __future__ import annotations

import asyncio
import uuid
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

# ── Imagine ────────────────────────────────────────────────────────────────
IMAGINE_NODE_GLOBAL_ID = "Phygital Creator/phygc-rnd-midjourney-api-imagine"
IMAGINE_NODE_NAME = "MidJourney"
IMAGINE_SCHEMA_ID = 82
SERVICE_VERSION = "0.0.68"

# ── Upscale (U1-U4) ────────────────────────────────────────────────────────
UPSCALE_NODE_GLOBAL_ID = "Phygital Creator/phygc-rnd-midjourney-api-upscale"
UPSCALE_NODE_NAME = "MJ Upscale"
UPSCALE_SCHEMA_ID = 85

# ── Variation (V1-V4) ──────────────────────────────────────────────────────
VARIATION_NODE_GLOBAL_ID = "Phygital Creator/phygc-rnd-midjourney-api-variation"
VARIATION_NODE_NAME = "MJ Variation"
VARIATION_SCHEMA_ID = 84


class MidJourneyImagineWorkflow(Workflow):
    """text→4 images через Midjourney /imagine. Возвращает 4 URL'а в result_urls."""

    workflow_id = str(IMAGINE_SCHEMA_ID)
    # MJ Imagine — ~60–120s по UI; берём 90s как медианную точку для % synth.
    EXPECTED_DURATION_S = 90.0

    def __init__(
        self,
        client: PhygitalClient,
        *,
        aspect_ratio: str = "square",
        model_version: str = "v7",
        chaos: int = 0,
        stylize: int = 100,
        style_weight: int = 100,
        omn_weight: int = 100,
        char_weight: int = 100,
        raw_mode: bool = True,
        tile: bool = False,
        weird: int = 0,
        image_weight: int = 1,
    ) -> None:
        super().__init__(client)
        self.aspect_ratio = aspect_ratio
        self.model_version = model_version
        self.chaos = chaos
        self.stylize = stylize
        self.style_weight = style_weight
        self.omn_weight = omn_weight
        self.char_weight = char_weight
        self.raw_mode = raw_mode
        self.tile = tile
        self.weird = weird
        self.image_weight = image_weight
        self._last_prompt: str = ""
        self._last_price: dict[str, Any] | None = None

    # ── payload ──────────────────────────────────────────────────────────
    def _params_list(self) -> list[dict[str, Any]]:
        return [
            {"name": "aspect_ratio", "type": "enum", "value": self.aspect_ratio, "meta": {}},
            {"name": "model_version", "type": "enum", "value": self.model_version, "meta": {}},
            {"name": "chaos", "type": "number", "value": self.chaos, "meta": {}},
            {"name": "stylize", "type": "number", "value": self.stylize, "meta": {}},
            {"name": "style_weight", "type": "number", "value": self.style_weight, "meta": {}},
            {"name": "omn_weight", "type": "number", "value": self.omn_weight, "meta": {}},
            {"name": "char_weight", "type": "number", "value": self.char_weight, "meta": {}},
            {"name": "raw_mode", "type": "bool", "value": self.raw_mode, "meta": {}},
            {"name": "tile", "type": "bool", "value": self.tile, "meta": {}},
            {"name": "weird", "type": "number", "value": self.weird, "meta": {}},
            {"name": "image_weight", "type": "number", "value": self.image_weight, "meta": {}},
        ]

    def _inputs_list(self, prompt: str) -> list[dict[str, Any]]:
        # 1-в-1 с recon: пустые init_img/style_reference как array, character/omni как image.
        return [
            {"name": "text_prompt", "type": "text", "optional": None,
             "isModified": False, "value": prompt, "meta": {}},
            {"name": "negative_prompt", "type": "text", "optional": True,
             "isModified": False, "value": "", "meta": {}},
            {"name": "init_img", "type": "array", "optional": None,
             "isModified": False, "value": [], "meta": {"dimensions": []}},
            {"name": "style_reference", "type": "array", "optional": None,
             "isModified": False, "value": [], "meta": {"dimensions": []}},
            {"name": "character_reference", "type": "image", "optional": True,
             "isModified": False, "value": "", "meta": {}},
            {"name": "omni_reference", "type": "image", "optional": True,
             "isModified": False, "value": "", "meta": {}},
        ]

    def build_payload(self, *, prompt: str) -> dict[str, Any]:
        self._last_prompt = prompt
        return {
            "id": IMAGINE_SCHEMA_ID,
            "inputs": self._inputs_list(prompt),
            "params": self._params_list(),
            "outputs": [{"name": "image_grid", "type": "array", "value": ""}],
        }

    def _build_config(self, prompt: str) -> dict[str, Any]:
        node_uuid = str(uuid.uuid4())
        node = {
            "globalId": IMAGINE_NODE_GLOBAL_ID,
            "name": IMAGINE_NODE_NAME,
            "uuid": node_uuid,
            "taskID": 0,
            "serviceVersion": SERVICE_VERSION,
            "inputSocketGroup": {
                "text_prompt": {
                    "name": "text_prompt", "type": "text", "value": prompt,
                    "optionalInfo": {"isEnabled": True, "mapOfEnabylity": {},
                                     "originalWorkspaceIds": prompt},
                },
                "negative_prompt": {
                    "name": "negative_prompt", "type": "text", "value": "",
                    "optionalInfo": {"isEnabled": True, "mapOfEnabylity": {},
                                     "originalWorkspaceIds": ""},
                },
                "init_img": {
                    "name": "init_img", "type": "array", "value": None,
                    "optionalInfo": {"isEnabled": True, "mapOfEnabylity": {},
                                     "originalWorkspaceIds": None},
                },
                "style_reference": {
                    "name": "style_reference", "type": "array", "value": None,
                    "optionalInfo": {"isEnabled": True, "mapOfEnabylity": {},
                                     "originalWorkspaceIds": None},
                },
                "character_reference": {
                    "name": "character_reference", "type": "image", "value": None,
                    "optionalInfo": {"isEnabled": True, "mapOfEnabylity": {},
                                     "originalWorkspaceIds": None},
                },
                "omni_reference": {
                    "name": "omni_reference", "type": "image", "value": None,
                    "optionalInfo": {"isEnabled": True, "mapOfEnabylity": {},
                                     "originalWorkspaceIds": None},
                },
            },
            "outputSocketGroup": [
                {
                    "name": "image_grid", "dataType": "array",
                    "optionalInfo": {
                        "valueOptions": {"itemType": {"dataType": "image"}},
                        "description": "Final result of AI creation",
                    },
                    "optional": None, "displayName": "Image", "value": [],
                }
            ],
            "meta": {
                "text_prompttextSelector": {"highlights": []},
                "negative_prompttextSelector": {"highlights": []},
                **({"taskPrice": self._last_price} if self._last_price else {}),
                "taskSchema": {
                    "id": IMAGINE_SCHEMA_ID,
                    "inputs": self._inputs_list(prompt),
                    "params": self._params_list(),
                    "outputs": [{"name": "image_grid", "type": "array", "value": ""}],
                },
            },
            "params": {
                p["name"]: {
                    "name": p["name"], "type": p["type"],
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

    # ── API calls ────────────────────────────────────────────────────────
    async def submit(self, payload: dict[str, Any]) -> str:
        try:
            price_payload = {
                "id": IMAGINE_SCHEMA_ID,
                "inputs": [{"name": "init_img", "value": None, "type": "image", "meta": {}}],
                "params": self._params_list(),
                "outputs": [],
            }
            self._last_price = await self.client.get_credits_price(price_payload)
            logger.debug(f"[mj-imagine] price: {self._last_price.get('price')}")
        except Exception as e:
            logger.warning(f"[mj-imagine] price lookup failed (non-fatal): {e}")

        task_id = await self.client.submit_task(payload)
        logger.info(f"[mj-imagine] Submitted task_id={task_id}")
        config = self._build_config(self._last_prompt)
        await self.client.post_config_history(task_id, config)
        logger.info(f"[mj-imagine] Posted config_history for task {task_id}")
        return str(task_id)

    async def wait(
        self,
        job_id: str,
        timeout: float = 300.0,
        poll_interval: float = 2.0,
    ) -> GenerationJob:
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
                    f"[mj-imagine] task {task_id}: {status} "
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
                        error="mj-imagine done but no output link_ids", raw=data,
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
                logger.warning(f"[mj-imagine] Unknown status '{status}', treating as pending")

            await asyncio.sleep(poll_interval)

        return GenerationJob(job_id=job_id, status="failed", error="timeout")


# ── Upscale / Variation (общий базовый класс — payload идентичен) ──────────
class _MJChildWorkflow(Workflow):
    """База для Upscale (id=85) и Variation (id=84) — обе принимают
    task_id+image_num и не имеют params."""

    SCHEMA_ID: int = 0
    NODE_GLOBAL_ID: str = ""
    NODE_NAME: str = ""
    LOG_PREFIX: str = "mj-child"
    EXPECTED_DURATION_S = 45.0

    def __init__(
        self,
        client: PhygitalClient,
        *,
        from_task_id: int,
        image_num: int,
    ) -> None:
        super().__init__(client)
        if not 1 <= image_num <= 4:
            raise ValueError(f"image_num must be 1..4, got {image_num}")
        self.from_task_id = int(from_task_id)
        self.image_num = int(image_num)
        self._last_price: dict[str, Any] | None = None

    def build_payload(self) -> dict[str, Any]:  # type: ignore[override]
        return {
            "id": self.SCHEMA_ID,
            "inputs": [
                {"name": "task_id", "value": str(self.from_task_id), "type": "text"},
                {"name": "image_num", "value": str(self.image_num), "type": "text"},
            ],
            "params": [],
            "outputs": [{"name": "image_grid", "type": "array", "value": ""}],
        }

    def _build_config(self) -> dict[str, Any]:
        node_uuid = str(uuid.uuid4())
        node = {
            "globalId": self.NODE_GLOBAL_ID,
            "name": self.NODE_NAME,
            "uuid": node_uuid,
            "serviceVersion": SERVICE_VERSION,
            "inputSocketGroup": {},
            "outputSocketGroup": [
                {
                    "name": "image_grid", "dataType": "array",
                    "optionalInfo": {"valueOptions": {"itemType": {"dataType": "image"}}},
                    "optional": None, "displayName": None, "value": [],
                }
            ],
            "meta": {
                "fromTaskID": self.from_task_id,
                "fromImageIndex": self.image_num - 1,
                "taskSchema": {
                    "id": self.SCHEMA_ID,
                    "inputs": [
                        {"name": "task_id", "value": str(self.from_task_id), "type": "text"},
                        {"name": "image_num", "value": str(self.image_num), "type": "text"},
                    ],
                    "params": [],
                    "outputs": [{"name": "image_grid", "type": "array", "value": ""}],
                },
                **({"taskPrice": self._last_price} if self._last_price else {}),
            },
            "params": {},
            "width": 350,
            "position": {"x": 600, "y": 200},
            "connections": [],
            "height": 184,
        }
        return {"nodes": [node], "executedNodeUuid": node_uuid}

    async def submit(self, payload: dict[str, Any]) -> str:
        try:
            price_payload = {
                "id": self.SCHEMA_ID,
                "inputs": [
                    {"name": "task_id", "value": str(self.from_task_id), "type": "text"},
                    {"name": "image_num", "value": str(self.image_num), "type": "text"},
                ],
                "params": [],
                "outputs": [],
            }
            self._last_price = await self.client.get_credits_price(price_payload)
            logger.debug(f"[{self.LOG_PREFIX}] price: {self._last_price.get('price')}")
        except Exception as e:
            logger.warning(f"[{self.LOG_PREFIX}] price lookup failed (non-fatal): {e}")

        task_id = await self.client.submit_task(payload)
        logger.info(
            f"[{self.LOG_PREFIX}] Submitted task_id={task_id} "
            f"(from={self.from_task_id}, image_num={self.image_num})"
        )
        config = self._build_config()
        await self.client.post_config_history(task_id, config)
        logger.info(f"[{self.LOG_PREFIX}] Posted config_history for task {task_id}")
        return str(task_id)

    async def wait(
        self,
        job_id: str,
        timeout: float = 240.0,
        poll_interval: float = 2.0,
    ) -> GenerationJob:
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
                    f"[{self.LOG_PREFIX}] task {task_id}: {status} "
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
                        error=f"{self.LOG_PREFIX} done but no output link_ids", raw=data,
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
                logger.warning(
                    f"[{self.LOG_PREFIX}] Unknown status '{status}', treating as pending"
                )

            await asyncio.sleep(poll_interval)

        return GenerationJob(job_id=job_id, status="failed", error="timeout")

    async def run(self, **_inputs: Any) -> GenerationJob:  # type: ignore[override]
        # Upscale/Variation не принимает kwargs — параметры в self.from_task_id/image_num.
        payload = self.build_payload()
        job_id = await self.submit(payload)
        return await self.wait(job_id)


class MJUpscaleWorkflow(_MJChildWorkflow):
    """U1-U4: ап-скейл одной из 4х картинок предыдущего MJ Imagine-таска."""

    workflow_id = str(UPSCALE_SCHEMA_ID)
    SCHEMA_ID = UPSCALE_SCHEMA_ID
    NODE_GLOBAL_ID = UPSCALE_NODE_GLOBAL_ID
    NODE_NAME = UPSCALE_NODE_NAME
    LOG_PREFIX = "mj-upscale"
    EXPECTED_DURATION_S = 30.0


class MJVariationWorkflow(_MJChildWorkflow):
    """V1-V4: вариации на основе одной из 4х картинок предыдущего MJ Imagine-таска."""

    workflow_id = str(VARIATION_SCHEMA_ID)
    SCHEMA_ID = VARIATION_SCHEMA_ID
    NODE_GLOBAL_ID = VARIATION_NODE_GLOBAL_ID
    NODE_NAME = VARIATION_NODE_NAME
    LOG_PREFIX = "mj-variation"
    EXPECTED_DURATION_S = 60.0
