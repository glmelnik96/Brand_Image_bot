"""
Управление system-prompt-документами для brand-сценариев.

System-prompt-файлы (.md в docs/) аплоадятся в Phygital storage один раз,
file_obj_id кэшируется в storage/brand_docs.json вместе с sha256 содержимого.
При изменении файла (sha256 != cached) — переаплоад.

text→image после split 2026-05-20 — три варианта (Photo / Render / Isometric),
выбираемых через `variant` в bot-сценарии. image→image остаётся монолитом.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

from loguru import logger

from client.api import PhygitalClient
from client.config import ROOT

DOCS_DIR = ROOT / "docs"
CACHE_FILE = ROOT / "storage" / "brand_docs.json"

# Phygital storage временно хранит залитые файлы; file_obj_id может протухнуть
# примерно через сутки (наблюдаемо: загрузка 20-05 19:58 → утром того же дня
# при последнем использовании ещё жива, через ~14h уже отдаёт «Cannot upload
# files»). Превентивно переаплоадим запись, если ей >12h — это покрывает TTL с
# хорошим запасом и сразу инвалидирует все старые кэши (без uploaded_at).
MAX_DOC_AGE_SECONDS = 12 * 3600

# text→image — 3 варианта брендового энхансера
PHOTO_DOC = "SYSTEM_PROMPT_Gemini3Pro_CloudRu_Photo_Enhancer.md"
RENDER_DOC = "SYSTEM_PROMPT_Gemini3Pro_CloudRu_Render_Enhancer.md"
ISOMETRIC_DOC = "SYSTEM_PROMPT_Gemini3Pro_CloudRu_Isometric_Enhancer.md"

# image→image — пока единый файл
IMG2IMG_DOC = "SYSTEM_PROMPT_Gemini3Pro_CloudRu_Img2Img_Enhancer.md"

# Nano Banana safety-reject fix-up. Не брендовый энхансер — хирургический cleaner,
# принимающий «PROMPT TO FIX + FLAGGED WORD» и возвращающий почищенный промпт.
SCRUBBER_DOC = "SYSTEM_PROMPT_Gemini3Pro_NanoBanana_Scrubber.md"

VARIANT_DOCS: dict[str, str] = {
    "photo": PHOTO_DOC,
    "render": RENDER_DOC,
    "isometric": ISOMETRIC_DOC,
}

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
        if (
            entry
            and entry.get("sha256") == digest
            and isinstance(entry.get("file_obj_id"), int)
            and isinstance(entry.get("uploaded_at"), (int, float))
            and (time.time() - float(entry["uploaded_at"])) < MAX_DOC_AGE_SECONDS
        ):
            logger.debug(f"brand_doc cache hit: {filename} → {entry['file_obj_id']}")
            return int(entry["file_obj_id"])

        if entry:
            age_h = (
                (time.time() - float(entry["uploaded_at"])) / 3600
                if isinstance(entry.get("uploaded_at"), (int, float))
                else None
            )
            logger.info(
                f"brand_doc cache stale: {filename} "
                f"(age_h={age_h!r}, had_id={entry.get('file_obj_id')}); re-uploading"
            )
        logger.info(f"brand_doc upload: {filename} (sha256={digest[:12]})")
        fid = await client.upload_file(path)
        cache[filename] = {
            "file_obj_id": fid,
            "sha256": digest,
            "name": filename,
            "uploaded_at": int(time.time()),
        }
        _save_cache(cache)
        logger.info(f"brand_doc cached: {filename} → file_obj_id={fid}")
        return fid


async def invalidate_brand_doc(filename: str) -> None:
    """Удалить запись из кэша. Используется, когда Phygital вернул
    'Cannot upload files' — file_obj_id протух раньше TTL, надо переаплоадить."""
    async with _lock:
        cache = _load_cache()
        removed = cache.pop(filename, None)
        if removed is not None:
            _save_cache(cache)
            logger.warning(
                f"brand_doc cache invalidated: {filename} "
                f"(was file_obj_id={removed.get('file_obj_id')})"
            )


async def get_text2img_doc(client: PhygitalClient, variant: str) -> int:
    """Вернуть file_obj_id для одного из трёх text→image энхансеров."""
    filename = VARIANT_DOCS.get(variant)
    if filename is None:
        raise ValueError(
            f"unknown brand text2img variant: {variant!r} "
            f"(допустимы: {sorted(VARIANT_DOCS)})"
        )
    return await get_brand_doc_id(client, filename)


async def get_img2img_doc(client: PhygitalClient) -> int:
    return await get_brand_doc_id(client, IMG2IMG_DOC)


async def get_scrubber_doc(client: PhygitalClient) -> int:
    """file_obj_id для Nano Banana safety-word scrubber'а."""
    return await get_brand_doc_id(client, SCRUBBER_DOC)
