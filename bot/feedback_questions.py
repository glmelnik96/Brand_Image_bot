"""Декларация опроса v1.

Каждый вопрос — одна экранная страница. Шаги опроса = `SURVEY_STEPS`,
плюс условные follow-up'ы (например, free-text после «нет, объясни» в C2).

Менять состав вопросов по живой версии нельзя — выкатывай v2 (поменяй
`SURVEY_VERSION` + добавь новый список), иначе у юзеров слетит привязка
ответов к ID. Старые ответы остаются в jsonl, дискриминируются полем
`survey_version`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SURVEY_VERSION = "v1"


@dataclass(frozen=True)
class Question:
    """Один экран опроса.

    kind:
      - "single":   один вариант из options
      - "multi":    несколько вариантов из options (toggle + «Дальше»)
      - "scale":    1..5 + опц. «не пользовался»
      - "free":     свободный текст (или «Пропустить»)
      - "single_free_on": single, но если выбран `free_on` — добавляется
                    follow-up free-text questionсодержательным id `<id>_free`.

    options:        list[(value, label)]; для scale — игнорируется.
    follow_up_free: True для scale / single_free_on — добавляет опциональный
                    free-text шаг ПОСЛЕ ответа (только если condition выполнено).
    """
    id: str
    text: str
    kind: str
    options: tuple[tuple[str, str], ...] = ()
    allow_none: bool = False
    none_value: str = "none"
    none_label: str = "Не пользовался"
    follow_up_free: bool = False
    free_on: str | None = None  # для "single_free_on": какой value триггерит free
    free_prompt: str = "Поясни (опционально, можешь пропустить):"


# Опрос v1 — 12 вопросов. ID совпадают с теми, что согласовали.
SURVEY_QUESTIONS: tuple[Question, ...] = (
    Question(
        id="A1",
        text="Какие сценарии используешь чаще всего?",
        kind="multi",
        options=(
            ("brand_photo", "Бренд / Photo"),
            ("brand_render", "Бренд / Render"),
            ("brand_iso", "Бренд / 2d Isometry"),
            ("generate", "Обычное изображение"),
            ("img2img", "Изменить изображение"),
            ("brand_img2img", "Добавить Brand patterns"),
            ("speaker", "Фотография спикера"),
        ),
        allow_none=True,
        none_value="none_used",
        none_label="Ни одним не пользовался",
    ),
    Question(
        id="A2",
        text="Какой сценарий из текущих — лишний / не используешь?",
        kind="single",
        options=(
            ("all_needed", "Все нужны"),
            ("brand_photo", "Бренд / Photo"),
            ("brand_render", "Бренд / Render"),
            ("brand_iso", "Бренд / 2d Isometry"),
            ("generate", "Обычное изображение"),
            ("img2img", "Изменить изображение"),
            ("brand_img2img", "Добавить Brand patterns"),
            ("speaker", "Фотография спикера"),
        ),
    ),
    Question(
        id="A3",
        text="Каких сценариев не хватает? Опиши кратко (одно сообщение).",
        kind="free",
    ),
    Question(
        id="A4",
        text="Под какие реальные задачи используешь бота?",
        kind="multi",
        options=(
            ("presentations", "Презентации"),
            ("social", "Соцсети"),
            ("landings", "Лендинги"),
            ("documents", "Документы"),
            ("prototypes", "Прототипы"),
            ("letters", "Письма / рассылки"),
            ("other", "Другое"),
        ),
    ),
    Question(
        id="A5",
        text="Решает ли бот задачу полностью или ты доделываешь руками?",
        kind="single",
        options=(
            ("full", "Полностью"),
            ("polish_minor", "Правлю мелочи"),
            ("base_only", "Использую как заготовку"),
            ("rework", "Приходится переделывать"),
        ),
    ),
    Question(
        id="B3",
        text="Со скольки попыток обычно получаешь подходящий результат?",
        kind="single",
        options=(
            ("1", "С первой"),
            ("2_3", "2–3"),
            ("4_6", "4–6"),
            ("gt_6", "Больше 6"),
            ("rare", "Редко получаю вообще"),
        ),
    ),
    Question(
        id="B5",
        text="Сценарий «Фотография спикера» — лицо узнаваемо, портрет похож?",
        kind="scale",
        allow_none=True,
        none_value="not_used",
        none_label="Не пользовался",
        follow_up_free=True,
        free_prompt="Что в портрете спикера не так / что нравится? (опционально)",
    ),
    Question(
        id="B6",
        text="Img2img / Brand patterns — сохраняет исходник как ты ожидал?",
        kind="scale",
        allow_none=True,
        none_value="not_used",
        none_label="Не пользовался",
        follow_up_free=True,
        free_prompt="Что в img2img не так / что нравится? (опционально)",
    ),
    Question(
        id="C1",
        text="Меню понятно с первого раза?",
        kind="single",
        options=(
            ("yes", "Да"),
            ("eventually", "Разобрался не сразу"),
            ("no", "До сих пор путаюсь"),
        ),
    ),
    Question(
        id="C2",
        text="Понятна ли разница Photo / Render / 2d Isometry?",
        kind="single_free_on",
        options=(
            ("yes", "Да, понятна"),
            ("partial", "Частично"),
            ("explain", "Нет, объясни"),
        ),
        free_on="explain",
        free_prompt="Что именно непонятно? (одно сообщение)",
    ),
    Question(
        id="E1",
        text="Как часто пользуешься ботом?",
        kind="single",
        options=(
            ("daily", "Ежедневно"),
            ("few_per_week", "2–3 раза в неделю"),
            ("weekly", "Раз в неделю"),
            ("rarely", "Реже"),
            ("once", "Разово попробовал"),
        ),
    ),
    Question(
        id="E5",
        text="Удобно ли получать результат именно в Telegram, или предпочёл бы другой канал?",
        kind="single",
        options=(
            ("tg_ok", "Telegram ок"),
            ("web", "Лучше веб-кабинет"),
            ("figma", "Лучше Figma-плагин"),
            ("email", "Лучше email"),
            ("slack_mm", "Лучше Slack/Mattermost"),
        ),
    ),
)


# Категории комментариев (точка «Оставить комментарий»).
COMMENT_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("bug", "Баг / сломалось"),
    ("idea", "Идея / новая фича"),
    ("quality", "Качество результатов"),
    ("scenario", "Проблема со сценарием"),
    ("general", "Комментарий"),
)


# Причины 👎-рейтинга (multi-select на отдельном экране).
RATING_REASONS: tuple[tuple[str, str], ...] = (
    ("not_brand", "Не похоже на бренд"),
    ("bad_face", "Плохое лицо"),
    ("artifacts", "Артефакты"),
    ("wrong_subject", "Не тот сюжет"),
    ("composition", "Композиция"),
    ("colors", "Цвета"),
    ("typo", "Шрифт / текст"),
    ("other", "Другое"),
)
