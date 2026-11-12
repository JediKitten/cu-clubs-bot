"""Stage 0 HTTP application: health checks and the generated OpenAPI document."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from cu_clubs import __version__
from cu_clubs.config import Settings

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_async_engine(
            config.database_url.get_secret_value(),
            pool_pre_ping=True,
            connect_args={"connect_timeout": 2},
        )
        app.state.db_engine = engine
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(
        title="CU Clubs — foundation",
        version=__version__,
        description=(
            "Stage 0 application foundation. Only health checks are implemented. "
            "Club, event, Telegram and integration workflows are planned."
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

    return app
