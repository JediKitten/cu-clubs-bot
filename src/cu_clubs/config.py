"""Environment configuration. Credentials never grant permissions by username."""

from pathlib import Path
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, PositiveInt, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CU_CLUBS_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    environment: Literal["local", "test", "production"] = "local"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://cu_clubs:local-dev-only@127.0.0.1:5432/cu_clubs"
    )
    readiness_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    timezone: str = "Europe/Moscow"

    # Numeric IDs only. These are configuration for a future explicit bootstrap command.
    # Loading Settings does not assign roles or write users to the database.
    bootstrap_admin_ids: list[PositiveInt] = Field(default_factory=list)

    external_delivery_enabled: bool = False
    telegram_bot_token: SecretStr | None = None
    telegram_webhook_secret: SecretStr | None = None

    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_sender: str | None = None

    google_auth_mode: Literal["oauth", "service_account"] = "oauth"
    google_credentials_path: Path | None = None
    google_spreadsheet_id: str | None = None
    google_sheet_name: str = "Calendar 2026"

    # Configuration only: stage 0 has no scheduler or backup delivery implementation.
    backup_recipient_telegram_id: PositiveInt | None = None
    backup_cron: str = "0 3 * * *"

    @field_validator("database_url")
    @classmethod
    def require_postgresql_psycopg(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
        except ArgumentError as exc:
            raise ValueError("DATABASE_URL must be a valid PostgreSQL URL") from exc
        if url.drivername != "postgresql+psycopg" or not url.database:
            raise ValueError("DATABASE_URL must use postgresql+psycopg and name a database")
        return value

    @field_validator("timezone")
    @classmethod
    def require_known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("TIMEZONE must be an IANA timezone name") from exc
        return value

    @model_validator(mode="after")
    def reject_external_delivery_in_stage_zero(self) -> Self:
        if self.external_delivery_enabled:
            raise ValueError(
                "External delivery is unavailable in stage 0; "
                "keep CU_CLUBS_EXTERNAL_DELIVERY_ENABLED=false"
            )
        return self
