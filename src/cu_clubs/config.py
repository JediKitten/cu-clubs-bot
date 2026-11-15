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
    self_service_pooler: bool = False
    readiness_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    timezone: str = "Europe/Moscow"

    # Numeric IDs only. Loading Settings never assigns roles or writes to the database.
    bootstrap_admin_ids: list[PositiveInt] = Field(default_factory=list)
    bootstrap_leader_telegram_id: PositiveInt | None = None
    test_allow_unverified_leaders: bool = False
    test_self_registration_enabled: bool = False

    external_delivery_enabled: bool = False
    telegram_bot_token: SecretStr | None = None
    telegram_webhook_secret: SecretStr | None = None
    telegram_bot_username: str | None = None
    telegram_webhook_url: str | None = None
    telegram_transport: Literal["webhook", "polling"] = "webhook"

    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_sender: str | None = None
    smtp_use_ssl: bool = False
    smtp_starttls: bool = True
    verification_secret: SecretStr | None = Field(default=None, min_length=32)

    google_auth_mode: Literal["oauth", "service_account"] = "oauth"
    google_credentials_path: Path | None = None
    google_spreadsheet_id: str | None = None
    google_sheet_name: str = "Calendar 2026"
    calendar_sync_enabled: bool = False
    calendar_allow_writes: bool = False
    calendar_test_spreadsheet_id: str | None = None
    calendar_sync_interval_seconds: int = Field(default=60, ge=15, le=3600)
    calendar_sync_batch_size: int = Field(default=10, ge=1, le=50)
    calendar_digest_color: str = Field(default="#FF00FF", pattern=r"^#[0-9A-Fa-f]{6}$")
    api_enabled: bool = True
    api_rate_limit_per_minute: int = Field(default=120, ge=1, le=10000)
    api_grant_ttl_days: int = Field(default=90, ge=1, le=365)

    # Daily backup time is interpreted in the configured timezone.
    backup_recipient_telegram_id: PositiveInt | None = None
    backup_cron: str = "0 3 * * *"
    backup_directory: Path = Path("backups")
    backups_enabled: bool = False

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
    def validate_delivery_credentials(self) -> Self:
        if self.test_self_registration_enabled and self.environment != "test":
            raise ValueError("Self registration is allowed only in the test environment")
        if self.test_allow_unverified_leaders and self.environment != "test":
            raise ValueError("Unverified leaders are allowed only in the test environment")
        if self.external_delivery_enabled:
            if not self.telegram_bot_token or not self.telegram_bot_username:
                raise ValueError("External delivery requires a Telegram token and bot username")
            if (
                not self.telegram_webhook_secret
                or len(self.telegram_webhook_secret.get_secret_value()) < 32
            ):
                raise ValueError("External delivery requires a webhook secret of at least 32 chars")
        if self.backups_enabled and (
            not self.external_delivery_enabled or not self.backup_recipient_telegram_id
        ):
            raise ValueError("Backups require external delivery and a numeric recipient ID")
        return self
