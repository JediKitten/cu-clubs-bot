"""HTTP health checks and authenticated, durable Telegram webhook ingestion."""

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from cu_clubs import __version__
from cu_clubs.config import Settings
from cu_clubs.db import make_database
from cu_clubs.inbox_crypto import seal_update
from cu_clubs.models import TelegramUpdate

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine, sessions = make_database(config)
        app.state.db_engine = engine
        app.state.sessions = sessions
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(
        title="CU Clubs",
        version=__version__,
        description=(
            "Club profiles, events and registrations through Telegram. "
            "Versioned club API uses club scopes and explicit user delegation."
        ),
        lifespan=lifespan,
    )

    @app.get("/health/live", tags=["health"])
    async def live() -> dict[str, str]:
        """Confirm the HTTP process is responding, independently of PostgreSQL."""
        return {"status": "alive"}

    @app.get(
        "/health/ready",
        tags=["health"],
        responses={503: {"description": "PostgreSQL is unavailable"}},
    )
    async def ready() -> JSONResponse:
        """Execute SELECT 1 against PostgreSQL; no domain migrations exist in stage 0."""
        try:
            async with asyncio.timeout(config.readiness_timeout_seconds):
                async with app.state.db_engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
        except (SQLAlchemyError, TimeoutError):
            # Driver exception strings can contain credentials and connection details.
            logger.warning("PostgreSQL readiness check failed")
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        return JSONResponse(content={"status": "ready"})

    @app.post("/telegram/webhook", include_in_schema=False)
    async def telegram_webhook(request: Request) -> dict[str, bool]:
        expected = config.telegram_webhook_secret
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if (
            not config.external_delivery_enabled
            or not expected
            or not secrets.compare_digest(expected.get_secret_value(), supplied)
        ):
            raise HTTPException(status_code=403, detail="Webhook access denied")
        # Reject oversized bodies before JSON decoding; never log raw updates or secrets.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1024 * 1024:
                raise HTTPException(status_code=413, detail="Update is too large")
        import json

        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
        update_id = payload.get("update_id") if isinstance(payload, dict) else None
        if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 0:
            raise HTTPException(status_code=422, detail="Invalid update ID")
        async with app.state.sessions() as session, session.begin():
            await session.execute(
                insert(TelegramUpdate)
                .values(
                    update_id=update_id, payload=seal_update(payload, config.verification_secret)
                )
                .on_conflict_do_nothing(index_elements=[TelegramUpdate.update_id])
            )
        return {"ok": True}

    from cu_clubs.api import install_api

    install_api(app, config)
    from cu_clubs.yandex_routes import install_yandex

    install_yandex(app, config)
    return app
