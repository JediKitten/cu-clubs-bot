import os

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from cu_clubs.app import create_app
from cu_clubs.config import Settings


def unavailable_database_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url=SecretStr("postgresql+psycopg://unused:never-log-this@127.0.0.1:1/missing"),
        readiness_timeout_seconds=1,
    )


def test_liveness_and_openapi_work_without_database() -> None:
    with TestClient(create_app(unavailable_database_settings())) as client:
        assert client.get("/health/live").json() == {"status": "alive"}
        response = client.get("/openapi.json")
        assert response.status_code == 200
        assert set(response.json()["paths"]) == {"/health/live", "/health/ready"}


def test_readiness_checks_database_and_hides_connection_details(caplog) -> None:
    with TestClient(create_app(unavailable_database_settings())) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert "never-log-this" not in caplog.text
    assert "127.0.0.1" not in response.text


@pytest.mark.integration
def test_readiness_with_real_postgresql() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set TEST_DATABASE_URL to run the real PostgreSQL readiness check")
    settings = Settings(_env_file=None, environment="test", database_url=SecretStr(database_url))
    with TestClient(create_app(settings)) as client:
        response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
