"""
Image-to-image workflow поверх Nano Banana (Phygital workflow id = 94).

Отличия от text-to-image (`image_gen.py`):
  - в `inputs.init_img.type` поле меняется с "array" на "image"
  - `inputs.init_img.value` — это **список file_obj_id** (порядок важен)
  - `inputs.init_img.meta.dimensions` — параллельный список {height, width}
  - дефолтные params под Nano Banana Pro 3.1: v3_1 / r_3_4 / k2

Flow:
  1. upload каждой init-картинки → file_obj_id (POST /api/v2/storage-object/...)
  2. submit задачи с init_img.value=[ids] и meta.dimensions=[{h,w}, ...]
  3. config_history (как в text-to-image — обязателен)
  4. polling + download links (логика наследуется от ImageGenWorkflow)
"""

from __future__ import annotations

import io
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps
from loguru import logger

# Регистрируем HEIF/HEIC opener в Pillow (iPhone-фото).
try:
    import pillow_heif  # type: ignore
    pillow_heif.register_heif_opener()
except ImportError:  # без HEIC всё ещё работаем на jpg/png/webp
    pass

from client.api import PhygitalClient
from workflows.image_gen import ImageGenWorkflow, WORKFLOW_SCHEMA_ID

# Phygital UI ресайзит большие входные картинки до ~2048 по длинной стороне
# и пересохраняет в jpeg перед заливкой. Имитируем это, иначе сервер обрывает
# крупные multipart-аплоады (ReadError на 30+МБ).
MAX_DIM = 2048
JPEG_QUALITY = 90


def _prepare_for_upload(path: Path) -> tuple[Path, dict[str, int], bool]:
    """Нормализация любой картинки под Phygital upload:
      - HEIC/HEIF, CMYK, P-палетные PNG, RGBA → конвертируем в RGB JPEG
      - EXIF Orientation учитываем (иначе портретные фото с телефона уходят на бок)
      - ресайз до MAX_DIM по длинной стороне
    Всегда пересохраняем во временный jpeg, кроме случая когда уже jpeg/RGB ≤MAX_DIM
    и без EXIF-rotation — тогда отдаём оригинал как есть.

    Возвращает (effective_path, dimensions, was_normalized).
    """
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)  # применяет orientation и убирает тег
        w, h = im.size
        ext = path.suffix.lower()
        is_jpeg_already = ext in {".jpg", ".jpeg"} and im.mode == "RGB"
        too_big = max(w, h) > MAX_DIM or path.stat().st_size > 6 * 1024 * 1024

        # Если уже подходящий JPEG/RGB, малого размера и orientation был тривиальный —
        # отдаём оригинал. Иначе — пересохраняем.
        if is_jpeg_already and not too_big and im.size == (w, h):
            return path, {"height": h, "width": w}, False

        # ресайз при необходимости
        if max(w, h) > MAX_DIM:
            scale = MAX_DIM / max(w, h)
            new_w, new_h = int(w * scale), int(h * scale)
            im = im.resize((new_w, new_h), Image.LANCZOS)
        else:
            new_w, new_h = w, h

        # цветовое пространство → RGB (jpeg не умеет alpha/CMYK/P)
        if im.mode != "RGB":
            if im.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", im.size, (255, 255, 255))
                im_rgba = im.convert("RGBA") if im.mode != "RGBA" else im
                bg.paste(im_rgba, mask=im_rgba.split()[-1])
                im = bg
            else:
                im = im.convert("RGB")

        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        tmp = Path(tempfile.mkstemp(suffix=".jpg", prefix=f"{path.stem}_norm_")[1])
        tmp.write_bytes(buf.getvalue())
        return tmp, {"height": new_h, "width": new_w}, True


class ImageToImageWorkflow(ImageGenWorkflow):
    """Nano Banana с init-картинками (img2img / multi-image reference).

    Кроме промпта принимает список путей к локальным файлам — они загружаются
    на бэк и подставляются как `init_img`.
    """

    def __init__(
        self,
        client: PhygitalClient,
        *,
        model_name: str = "v3_1",
        ratio: str = "r_3_4",
        resolution: str = "k2",
    ) -> None:
        super().__init__(
            client,
            model_name=model_name,
            ratio=ratio,
            resolution=resolution,
        )
        # заполняется в submit_with_files(); используется build_payload/_build_config
        self._init_img_ids: list[int] = []
        self._init_img_dims: list[dict[str, int]] = []

    # ── payload (override) ────────────────────────────────────────────────
    def build_payload(self, *, prompt: str, init_img: list[Any] | None = None) -> dict[str, Any]:
        self._last_prompt = prompt
        # init_img-аргумент игнорируем: значения берём из state, заполненного upload-шагом.
        return {
            "id": WORKFLOW_SCHEMA_ID,
            "inputs": [
                {"name": "text_prompt", "type": "text", "optional": None,
                 "isModified": False, "value": prompt, "meta": {}},
                {"name": "init_img", "type": "image", "optional": None,
                 "isModified": False,
                 "value": list(self._init_img_ids),
                 "meta": {"dimensions": list(self._init_img_dims)}},
            ],
            "params": self._params_list(),
            "outputs": [{"name": "image", "type": "array", "value": ""}],
        }

    # ── config_history (override only the init_img piece in taskSchema) ───
    def _build_config(self, prompt: str) -> dict[str, Any]:
        cfg = super()._build_config(prompt)
        node = cfg["nodes"][0]
        # подменяем init_img внутри meta.taskSchema на img2img-форму
        task_inputs = node["meta"]["taskSchema"]["inputs"]
        for i, inp in enumerate(task_inputs):
            if inp.get("name") == "init_img":
                task_inputs[i] = {
                    "name": "init_img",
                    "type": "image",
                    "optional": None,
                    "isModified": False,
                    "value": list(self._init_img_ids),
                    "meta": {"dimensions": list(self._init_img_dims)},
                }
                break
        return cfg

    # ── high-level entrypoint ─────────────────────────────────────────────
    async def upload_images(self, paths: list[str | Path]) -> tuple[list[int], list[dict[str, int]]]:
        ids: list[int] = []
        dims: list[dict[str, int]] = []
        for p in paths:
            pp = Path(p).expanduser().resolve()
            if not pp.exists():
                raise FileNotFoundError(pp)
            effective, dim, normalized = _prepare_for_upload(pp)
            if normalized:
                logger.info(
                    f"normalized {pp.name} → {dim['width']}x{dim['height']} "
                    f"({effective.stat().st_size/1024:.0f}KB jpeg)"
                )
            try:
                fid = await self.client.upload_file(effective)
            finally:
                if normalized and effective != pp:
                    try:
                        effective.unlink()
                    except OSError:
                        pass
            logger.info(f"uploaded {pp.name} → file_obj_id={fid}  ({dim['width']}x{dim['height']})")
            ids.append(fid)
            dims.append(dim)
        return ids, dims

    async def run_with_files(
        self,
        *,
        prompt: str,
        init_paths: list[str | Path],
    ):
        """Загружает картинки, затем стандартный submit/wait."""
        self._init_img_ids, self._init_img_dims = await self.upload_images(init_paths)
        return await self.run(prompt=prompt)
