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

    redis_url: str = "redis://localhost:6379/0"
    session_file: Path = ROOT / "storage" / "session.json"

    log_level: str = "INFO"

    @property
    def allowed_user_ids(self) -> set[int]:
        ids = (x.strip() for x in self.telegram_allowed_user_ids.split(","))
        return {int(x) for x in ids if x}


settings = Settings()
