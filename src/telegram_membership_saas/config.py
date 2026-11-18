from functools import lru_cache
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
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
    # Explicit Telegram bot IDs only; default off for every real buyer.
    rich_file_checkout_bot_ids: str = ""
    # Subscription Rich offer cards are independent of file-sale Rich cards.
    rich_subscription_checkout_bot_ids: str = ""
    # Project-wide Rich UI. Set false to restore classic HTML for all bots.
    rich_messages_enabled: bool = True
    # Fail closed for local/standalone runners. The opt-in light100 release
    # overlay enables buyer checkout on the supervised staging deployment.
    # This does NOT activate platform SaaS invoices/holds, and turning it off
    # never cancels settlement or recovery for already payable invoices.
    crypto_pay_buyer_enabled: bool = False
    customer_supervisor_max_bots: int = Field(default=10, ge=1, le=128)
    customer_supervisor_lease_mode: Literal["per_project", "shared"] = "per_project"
    customer_supervisor_shard_count: int = Field(default=1, ge=1, le=256)
    customer_supervisor_shard_id: int = Field(default=0, ge=0, le=255)
    default_project_timezone: str = "Europe/Moscow"
    platform_owner_telegram_id: int | None = None
    # Central Stars service settlement must never use a connected tenant bot.
    saas_central_bot_username: str = "vimipay_bot"
    # Platform-owned billing credentials; never reuse tenant merchant apps.
    # Unset by default: no automatic SaaS external checkout without secrets.
    saas_crypto_pay_api_token: SecretStr | None = None
    saas_crypto_pay_testnet: bool = False
    saas_lava_top_api_key: SecretStr | None = None
    saas_lava_top_offer_id: UUID | None = None
    # Switch on only after the isolated public redirect API and HTTPS route pass smoke tests.
    saas_shortlinks_enabled: bool = False
    platform_timezone: str = "Europe/Moscow"
    # Explicit opt-in, only for central platform owner monitoring.
    platform_backup_gcs_bucket: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="TMS_",
        extra="ignore",
        case_sensitive=False,
    )

    @model_validator(mode="after")
    def validate_supervisor_shard(self) -> "Settings":
        if self.customer_supervisor_shard_id >= self.customer_supervisor_shard_count:
            raise ValueError("Supervisor shard ID must be smaller than shard count")
        if (self.customer_supervisor_lease_mode == "per_project"
                and self.customer_supervisor_max_bots > 32):
            raise ValueError(
                "More than 32 pollers require opt-in shared supervisor leases"
            )
        return self

    @field_validator("bot_token_encryption_key", mode="before")
    @classmethod
    def normalize_empty_encryption_key(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "saas_lava_top_api_key", "saas_crypto_pay_api_token", mode="before",
    )
    @classmethod
    def normalize_empty_platform_payment_token(cls, value: object) -> object:
        # Compose substitutes an empty string when staging secrets are unset.
        # An empty SecretStr must not start false provider reconciliation.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("saas_lava_top_offer_id", mode="before")
    @classmethod
    def normalize_empty_platform_lava_offer(cls, value: object) -> object:
        # An unset UUID must not prevent the entire central bot from starting.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("telegram_project_id", mode="before")
    @classmethod
    def normalize_empty_telegram_project_id(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("platform_owner_telegram_id", mode="before")
    @classmethod
    def normalize_empty_platform_owner_id(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("saas_central_bot_username")
    @classmethod
    def validate_central_bot_username(cls, value: str) -> str:
        import re
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", value):
            raise ValueError("Invalid central platform bot username")
        return value

    @field_validator("platform_owner_telegram_id")
    @classmethod
    def validate_platform_owner_id(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("Platform owner Telegram ID must be positive")
        return value

    @field_validator("platform_backup_gcs_bucket", mode="before")
    @classmethod
    def validate_backup_bucket(cls, value: object) -> object:
        import re

        if value is None or value == "":
            return None
        if not isinstance(value, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9._-]{2,61}", value
        ):
            raise ValueError("Invalid GCS backup bucket")
        return value

    @field_validator("platform_timezone")
    @classmethod
    def validate_platform_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown IANA timezone: {value}") from exc
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
