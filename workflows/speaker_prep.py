"""
Speaker-prep пресет: подготовка портрета спикера в едином стиле
(чёрное худи на белом фоне, 3/4 поза, ключевая поза из reference-кадра).

Использует ImageToImageWorkflow (Nano Banana 3.1, ratio 3:4, 2K).

Дополнительно: helper для смены фона на готовом портрете — один и тот же снимок
уходит в Nano Banana без других вложений, новый фон выбирается из BG_COLORS.

Пример:
    python -m cli prep-speaker input/speaker\\ example_woman.jpg
        --reference input/input\\ speaker\\ image.png
        --gender woman
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from client.api import PhygitalClient
from workflows.image_to_image import ImageToImageWorkflow

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REFERENCE = ROOT / "input" / "input speaker image.png"

Gender = Literal["man", "woman"]


# Брендовые цвета фона для after-speaker сценария «Сменить фон».
# Порядок отражается в кнопках под результатом prep_speaker.
BG_COLORS: list[tuple[str, str]] = [
    ("26D07C", "Зелёный"),
    ("CFF500", "Лайм"),
    ("A068FF", "Фиолетовый"),
    ("C0E0FC", "Голубой"),
    ("222222", "Чёрный"),
]


# Промпты переписаны 2026-05-22: явный запрет рисовать red square из reference,
# плюс жёсткое сохранение identity лица + лёгкие вариации складок худи/капюшона.
# man/woman различаются ровно одним словом (subject), остальной текст идентичен.
def _prompt(gender: Gender) -> str:
    subject = "woman" if gender == "woman" else "man"
    return (
        f"Create a photorealistic studio portrait of the {subject} from the face "
        "reference, preserving the same identity, facial structure, hairstyle, skin "
        "tone, and natural proportions.\n\n"
        "Use the reference image with the red square only as a guide for composition, "
        "pose, framing, wardrobe, and logo placement.\n"
        "The red square is only a reference marker and must not appear in the final image.\n"
        "No red square, no colored square, no annotation marker, no overlay box, no "
        "graphic element floating in the background.\n\n"
        "Match the reference image in:\n"
        "- same waist-up framing\n"
        "- same strict three-quarter body angle\n"
        "- same head position\n"
        "- direct eye contact with the camera\n"
        "- same clean white seamless background\n\n"
        "The black hoodie must match the reference image in design, fit, thickness, "
        "silhouette, and centered chest logo placement.\n\n"
        "Allow only subtle natural variation in the hoodie folds and fabric drape:\n"
        "- slightly different wrinkle patterns\n"
        "- slightly different sleeve creases\n"
        "- slightly different natural fabric tension\n"
        "- realistic gravity-based folds\n"
        "Do not copy the exact fold pattern from the reference image.\n\n"
        "The hood must remain the same type and shape as in the reference hoodie, but "
        "its fold pattern should vary slightly in every new generation:\n"
        "- slightly different hood opening shape\n"
        "- slightly different hood drape around the neck and shoulders\n"
        "- slightly different fold compression around the collar\n"
        "- slightly different hood tension and fabric overlap\n"
        "- realistic, non-repetitive hood folds from one generation to another\n"
        "Do not repeat the exact same hood position or identical hood wrinkles across "
        "generations.\n\n"
        "Keep the logo centered, clean, readable, and naturally following the fabric "
        "surface.\n\n"
        "Use soft professional studio lighting, realistic anatomy, correct "
        "head-to-body proportions, natural skin texture, sharp focus on the eyes and "
        "face, neutral color rendering, and a clean photographic look.\n\n"
        "Preserve the face identity from the face reference.\n"
        "Preserve the pose, framing, hoodie design, and logo placement from the "
        "reference image.\n"
        "Allow subtle natural variation in the hoodie folds and especially in the hood "
        "folds.\n"
        "Do not reproduce any visual annotations or markers from the reference."
    )


def bg_swap_prompt(hex_color: str, color_name: str) -> str:
    """Prompt для смены фона на однотонный заданного цвета. Сабжект, поза, одежда,
    свет — без изменений. Используется как nb_i2i с одной init-картинкой (результат
    prep_speaker), без других вложений."""
    h = hex_color.strip().lstrip("#").upper()
    return (
        f"Replace ONLY the background of this photo with a solid, uniform, flat color "
        f"#{h} ({color_name}).\n\n"
        "Keep the subject (person, clothing, hair, skin tone, pose, facial features, "
        "facial expression, eye direction, accessories, logo placement) pixel-identical "
        "to the input image.\n"
        "- Do not change the face, identity, skin tone, or facial features.\n"
        "- Do not modify hair style, hair color, or hair position.\n"
        "- Do not modify the hoodie, its folds, fabric texture, or the chest logo.\n"
        "- Do not change body pose, framing, angle, or proportions.\n"
        "- Do not change the lighting or shading on the subject.\n"
        "- Keep edges around hair, ears, and shoulders clean and natural — no halos, "
        "no fringing, no color bleed from the background.\n\n"
        "The new background must be:\n"
        f"- a single uniform solid color #{h} ({color_name}), perfectly flat\n"
        "- no gradient, no vignette, no texture, no noise, no shadow on the background "
        "plane\n"
        "- edge-to-edge full coverage of the frame behind the subject\n\n"
        f"Output a high-resolution photographic portrait with the exact same subject and "
        f"the new flat #{h} background."
    )


def build_speaker_prep_workflow(client: PhygitalClient) -> ImageToImageWorkflow:
    """Nano Banana Pro 3.1, 3:4, 2K — параметры из recon."""
    return ImageToImageWorkflow(
        client,
        model_name="v3_1",
        ratio="r_3_4",
        resolution="k2",
    )


def speaker_prompt(gender: Gender) -> str:
    return _prompt(gender)


def detect_gender_from_filename(path: Path) -> Gender | None:
    name = path.name.lower()
    if "woman" in name or "female" in name:
        return "woman"
    if "man" in name or "male" in name:
        return "man"
    return None
