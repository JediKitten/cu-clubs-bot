import pytest
from pydantic import ValidationError

from cu_clubs.config import Settings


def test_local_start_needs_no_integration_credentials() -> None:
    settings = Settings(_env_file=None)
    assert settings.telegram_bot_token is None
    assert settings.external_delivery_enabled is False
    assert settings.bootstrap_admin_ids == []
    assert settings.backup_cron == "0 3 * * *"
    assert settings.timezone == "Europe/Moscow"


def test_admin_ids_accept_only_numeric_ids_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("CU_CLUBS_BOOTSTRAP_ADMIN_IDS", "[123456789, 987654321]")
    assert Settings(_env_file=None).bootstrap_admin_ids == [123456789, 987654321]
    monkeypatch.setenv("CU_CLUBS_BOOTSTRAP_ADMIN_IDS", '["@jedi_kitten"]')
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_external_delivery_fails_closed() -> None:
    with pytest.raises(ValidationError, match="External delivery is unavailable"):
        Settings(_env_file=None, external_delivery_enabled=True)


def test_database_must_be_postgresql_and_errors_hide_credentials() -> None:
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, database_url="mysql://user:very-secret@db/database")
    assert "postgresql+psycopg" in str(error.value)
    assert "very-secret" not in str(error.value)


def test_empty_optional_environment_values_are_unset(monkeypatch) -> None:
    monkeypatch.setenv("CU_CLUBS_BACKUP_RECIPIENT_TELEGRAM_ID", "")
    monkeypatch.setenv("CU_CLUBS_TELEGRAM_BOT_TOKEN", "")
    settings = Settings(_env_file=None)
    assert settings.backup_recipient_telegram_id is None
    assert settings.telegram_bot_token is None


def test_invalid_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError, match="IANA timezone"):
        Settings(_env_file=None, timezone="Not/A_Timezone")
