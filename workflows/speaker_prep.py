"""
Speaker-prep пресет: подготовка портрета спикера в едином стиле
(чёрное худи на белом фоне, 3/4 поза, ключевая поза из reference-кадра).

Использует ImageToImageWorkflow (Nano Banana 3.1, ratio 3:4, 2K) и зашитые
промпты из последнего успешного recon-сценария.

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


# Промпты — точные строки из recon (HAR), словесно различаются только man/woman.
def _prompt(gender: Gender) -> str:
    subject = "young woman" if gender == "woman" else "young man"
    person = "woman" if gender == "woman" else "man"
    return (
        f"A classic portrait of a {subject} with studio lighting, medium shot. "
        f"The {person} enters the frame from the waist up, wearing a black hoodie, "
        "against a perfectly white background. The pose and body angle must perfectly "
        "match the reference photo with the red square, strictly maintaining a three-quarter "
        "view. Photorealistic, natural studio photography.\n\n"
        "Maintain the same natural anatomical balance and realistic body proportions as in "
        "the reference image. Do not enlarge or shrink the head unnaturally. No distorted "
        "anatomy, no exaggerated head size, no compressed or stretched body proportions.\n\n"
        "Realistic skin texture with visible pores and fine microtexture. Natural imperfections "
        "are barely noticeable. No retouching, no excessive smoothing or artificial skin. "
        "Detailed facial features. Natural matte skin with a subtle, realistic sheen. \n\n"
        "Textured black hoodie with visible weave and a soft cotton texture with logos in the "
        "center that perfectly follow the folds. The geometry and placement of the fabric folds "
        "must be randomized and naturally different from the reference photo in every generation. "
        "Create unique, dynamically generated creases, draping, and seam details each time while "
        "maintaining realistic fabric weight and physics. \n\n"
        "Clean, white, seamless background. Accurate proportions. Realistic color rendition. "
        "Sharp focus on the face. A highly detailed professional portrait.\n\n"
        "Ultra-realistic photography. Exact three-quarter pose. Realistic skin detail. "
        "Visible pores. Fine microtexture of the face. Natural fabric texture. "
        "Unique, non-repetitive fabric folds. No skin smoothing. No waxy skin. "
        "No excessive retouching."
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
