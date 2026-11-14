"""Alembic migration runner. Credentials come from environment settings, never ini."""

import asyncio
from importlib import import_module

from alembic import context
from sqlalchemy import Connection

from cu_clubs.config import Settings
from cu_clubs.db import make_database
from cu_clubs.models import Base

# Register every mapped table for metadata comparison as well as explicit upgrades.
for module_name in (
    "event_models",
    "identity_models",
    "attendance_models",
    "calendar_models",
    "survey_models",
    "api_models",
    "notification_models",
    "screens",
):
    import_module("cu_clubs." + module_name)

config = context.config
target_metadata = Base.metadata


def run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine, _ = make_database(Settings())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=Settings().database_url.get_secret_value(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
elif config.attributes.get("connection") is not None:
    run_migrations(config.attributes["connection"])
else:
    asyncio.run(run_online())
