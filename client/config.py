"""Конфиг через pydantic-settings + .env."""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    phygital_base_url: str = "https://app.phygital.plus"
    phygital_email: str = ""
    phygital_password: str = ""

    telegram_bot_token: str = ""
    telegram_allowed_user_ids: str = ""  # CSV; парсим в свойстве
    telegram_proxy_url: str = ""  # http://host:port; пусто = без прокси
    telegram_proxy_cert: str = ""  # path к PEM CA прокси; пусто = truststore (keychain)
    bot_max_concurrency: int = 5
    # CSV uid'ов, которым доступна админская статистика по фидбэку.
    bot_owner_uids: str = ""
    # CSV usernames (без @) ботов, которым разрешён b2b-канал (@b2b ... messages).
    # Пустая строка = b2b-канал закрыт для всех.
    b2b_bot_whitelist: str = ""
    # Максимум параллельных b2b-задач (служебный канал, отдельный от user-семафора).
    b2b_max_concurrency: int = 2
    # Тайм-аут одной b2b-генерации, секунды (Resize_bot ждёт 600).
    b2b_request_timeout_sec: int = 600
    # Remove Background по b2b: @b2b removebg (фото/документ+caption) и chained
    # rmbg=1 у генерации. Фича-гейт: false = removebg-канал закрыт.
    b2b_removebg_enabled: bool = False
    # Отдельный семафор для removebg — нода Photoroom быстрая/дешёвая (~15s),
    # не должна стоять в очереди за тяжёлой генерацией и наоборот.
    b2b_removebg_max_concurrency: int = 4
    # Тайм-аут одной removebg-операции, секунды.
    b2b_removebg_timeout_sec: int = 120

    session_file: Path = ROOT / "storage" / "session.json"

    log_level: str = "INFO"

    @property
    def allowed_user_ids(self) -> set[int]:
        ids = (x.strip() for x in self.telegram_allowed_user_ids.split(","))
        return {int(x) for x in ids if x}

    @property
    def owner_user_ids(self) -> set[int]:
        ids = (x.strip() for x in self.bot_owner_uids.split(","))
        return {int(x) for x in ids if x}

    @property
    def b2b_whitelist(self) -> set[str]:
        return {
            s.strip().lstrip("@").lower()
            for s in self.b2b_bot_whitelist.split(",")
            if s.strip()
        }


settings = Settings()
