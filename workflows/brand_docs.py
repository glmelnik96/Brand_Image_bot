"""
Управление system-prompt-документами для brand-сценариев.

System-prompt-файлы (.md в docs/) аплоадятся в Phygital storage один раз,
file_obj_id кэшируется в storage/brand_docs.json вместе с sha256 содержимого.
При изменении файла (sha256 != cached) — переаплоад.

Используется brand_text2img / brand_img2img как input `documents` для Gemini Text.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from loguru import logger

from client.api import PhygitalClient
from client.config import ROOT

DOCS_DIR = ROOT / "docs"
CACHE_FILE = ROOT / "storage" / "brand_docs.json"

# Имена .md в docs/ → ключ в кэше
TEXT2IMG_DOC = "SYSTEM_PROMPT_Gemini3Pro_CloudRu_Enhancer.md"
IMG2IMG_DOC = "SYSTEM_PROMPT_Gemini3Pro_CloudRu_Img2Img_Enhancer.md"

_lock = asyncio.Lock()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _load_cache() -> dict[str, dict]:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"brand_docs cache unreadable ({e}), starting fresh")
        return {}


def _save_cache(cache: dict[str, dict]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


async def get_brand_doc_id(client: PhygitalClient, filename: str) -> int:
    """Вернуть file_obj_id для system-prompt .md из docs/.
    Аплоадит при первом обращении или при изменении содержимого."""
    path = DOCS_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"brand system_prompt not found: {path}")

    digest = _sha256(path)
    async with _lock:
        cache = _load_cache()
        entry = cache.get(filename)
        if entry and entry.get("sha256") == digest and isinstance(entry.get("file_obj_id"), int):
            logger.debug(f"brand_doc cache hit: {filename} → {entry['file_obj_id']}")
            return int(entry["file_obj_id"])

        logger.info(f"brand_doc upload: {filename} (sha256={digest[:12]})")
        fid = await client.upload_file(path)
        cache[filename] = {"file_obj_id": fid, "sha256": digest, "name": filename}
        _save_cache(cache)
        logger.info(f"brand_doc cached: {filename} → file_obj_id={fid}")
        return fid


async def get_text2img_doc(client: PhygitalClient) -> int:
    return await get_brand_doc_id(client, TEXT2IMG_DOC)


async def get_img2img_doc(client: PhygitalClient) -> int:
    return await get_brand_doc_id(client, IMG2IMG_DOC)
