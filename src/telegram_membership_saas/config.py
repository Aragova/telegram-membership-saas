from functools import lru_cache
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Telegram Membership SaaS"
    environment: Literal["development", "test", "staging", "production"] = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://membership:membership@localhost:5432/membership"
    migration_database_url: str | None = None
    telegram_bot_token: SecretStr | None = None
    bot_token_encryption_key: SecretStr | None = None
    telegram_project_id: UUID | None = None
    customer_telegram_bot_id: int | None = None
    telegram_central_access_enabled: bool = True
    telegram_worker_poll_seconds: float = 1.0
    default_project_timezone: str = "Europe/Moscow"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="TMS_",
        extra="ignore",
        case_sensitive=False,
    )

    @field_validator("bot_token_encryption_key", mode="before")
    @classmethod
    def normalize_empty_encryption_key(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("telegram_project_id", mode="before")
    @classmethod
    def normalize_empty_telegram_project_id(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("default_project_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown IANA timezone: {value}") from exc
        return value

    @field_validator("customer_telegram_bot_id", mode="before")
    @classmethod
    def normalize_empty_customer_bot_id(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("customer_telegram_bot_id")
    @classmethod
    def validate_customer_bot_id(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("customer Telegram bot ID must be positive")
        return value

    @field_validator("telegram_worker_poll_seconds")
    @classmethod
    def validate_worker_poll_seconds(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("telegram_worker_poll_seconds must be positive")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
