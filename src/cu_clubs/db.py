"""Database construction; schema changes are performed by Alembic."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from cu_clubs.config import Settings


def make_database(settings: Settings) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
    )
    return engine, async_sessionmaker(
        engine,
        expire_on_commit=False,
        info={"test_self_registration_enabled": settings.test_self_registration_enabled},
    )
