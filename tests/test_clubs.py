"""Real PostgreSQL acceptance checks, isolated in a fresh schema for each test.

Set TEST_DATABASE_URL to a disposable PostgreSQL database. No external delivery is used.
"""

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from pydantic import SecretStr
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from cu_clubs.config import Settings
from cu_clubs.db import make_database
from cu_clubs.models import (
    Audit,
    Base,
    Club,
    ClubLevel,
    Conversation,
    GlobalAdmin,
    Membership,
    Outbox,
    Person,
    PublicTeam,
    Subscription,
    TelegramAccount,
    TelegramUpdate,
)
from cu_clubs.services import ClubService, PermissionDenied, ValidationError

pytestmark = pytest.mark.integration
ADMIN, LEADER, MEMBER, STRANGER = 100001, 100002, 100003, 100004
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def database_url():
    configured = os.environ.get("TEST_DATABASE_URL")
    if not configured:
        pytest.skip("Set TEST_DATABASE_URL to run real PostgreSQL club checks")
    schema = f"test_clubs_{uuid4().hex}"
    engine = create_engine(configured)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    scoped = make_url(configured).update_query_dict({"options": f"-csearch_path={schema}"})
    try:
        yield scoped.render_as_string(hide_password=False)
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def run(database_url, scenario):
    async def exercise():
        settings = Settings(_env_file=None, database_url=SecretStr(database_url))
        engine, sessions = make_database(settings)
        try:
            await scenario(sessions)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


async def seed(sessions):
    async with sessions.begin() as session:
        return await ClubService(session).bootstrap([ADMIN], LEADER)


async def add_person(sessions, telegram_id=MEMBER, username=None):
    async with sessions.begin() as session:
        return await ClubService(session).get_or_create_person(telegram_id, "Test User", username)


def test_migration_matches_metadata_and_roundtrips(database_url):
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
            assert set(inspect(connection).get_table_names()) == {
                *Base.metadata.tables,
                "alembic_version",
            }
            config = Config(str(PROJECT_ROOT / "alembic.ini"))
            config.attributes["connection"] = connection
            command.downgrade(config, "base")
            assert inspect(connection).get_table_names() == ["alembic_version"]
            command.upgrade(config, "head")
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    finally:
        engine.dispose()


def test_bootstrap_is_idempotent_and_does_not_verify_email(database_url):
    async def scenario(sessions):
        first = await seed(sessions)
        async with sessions.begin() as session:
            service = ClubService(session)
            await service.update_club(LEADER, "cinema", "name", "Edited cinema")
        again = await seed(sessions)
        assert first["id"] == again["id"]
        assert again["name"] == "Edited cinema"
        assert again["public_id"] == "cinema" and again["level"] == "Seed"
        async with sessions.begin() as session:
            for model, count in [
                (Club, 1),
                (ClubLevel, 5),
                (Person, 2),
                (GlobalAdmin, 1),
                (Membership, 1),
                (Outbox, 1),
            ]:
                assert await session.scalar(select(func.count()).select_from(model)) == count
            assert all(
                person.verified_at is None for person in await session.scalars(select(Person))
            )
            message = await session.scalar(select(Outbox))
            assert message.kind == "club.created" and message.recipient_telegram_id == LEADER
            assert message.payload == {
                "club_id": first["id"],
                "public_id": "cinema",
                "name": "Киноклуб",
            }
            assert message.status == "pending" and message.attempts == 0
            assert await ClubService(session).is_admin(ADMIN)
            assert not await ClubService(session).is_admin(LEADER)

    run(database_url, scenario)


def test_concurrent_bootstrap_creates_one_club_and_notification(database_url):
    async def scenario(sessions):
        results = await asyncio.gather(*(seed(sessions) for _ in range(4)))
        assert len({item["id"] for item in results}) == 1
        async with sessions.begin() as session:
            assert await session.scalar(select(func.count()).select_from(Outbox)) == 1
            assert await session.scalar(select(func.count()).select_from(Audit)) == 2

    run(database_url, scenario)


def test_rights_are_independent_public_team_does_not_grant_access(database_url):
    async def scenario(sessions):
        await seed(sessions)
        await add_person(sessions)
        async with sessions.begin() as session:
            service = ClubService(session)
            await service.update_club(
                LEADER, "cinema", "public_team", [{"name": "Public face", "role": "Host"}]
            )
            assert not (await service.permissions(MEMBER, "cinema"))["edit_profile"]
            await service.set_organizer(
                LEADER,
                "cinema",
                MEMBER,
                edit_profile=False,
                manage_events=True,
                record_attendance=False,
            )
        async with sessions.begin() as session:
            service = ClubService(session)
            permissions = await service.permissions(MEMBER, "cinema")
            assert permissions == {
                "is_admin": False,
                "is_leader": False,
                "edit_profile": False,
                "manage_events": True,
                "record_attendance": False,
            }
            assert len(await service.managed_clubs(MEMBER)) == 1
            await service.authorize(MEMBER, "cinema", "manage_events")
            with pytest.raises(PermissionDenied):
                await service.update_club(MEMBER, "cinema", "name", "Forbidden")
            with pytest.raises(PermissionDenied):
                await service.set_organizer(MEMBER, "cinema", ADMIN)
        async with sessions.begin() as session:
            await ClubService(session).set_organizer(
                ADMIN,
                "cinema",
                MEMBER,
                edit_profile=True,
                manage_events=False,
                record_attendance=True,
            )
        async with sessions.begin() as session:
            service = ClubService(session)
            permissions = await service.permissions(MEMBER, "cinema")
            assert permissions["edit_profile"] and permissions["record_attendance"]
            assert not permissions["manage_events"]
            await service.update_club(MEMBER, "cinema", "description", "Allowed")
            assert (await service.get_club("cinema"))["public_team"] == [
                {"name": "Public face", "role": "Host"}
            ]
        async with sessions.begin() as session:
            await ClubService(session).remove_organizer(ADMIN, "cinema", MEMBER)
        async with sessions.begin() as session:
            assert not (await ClubService(session).permissions(MEMBER, "cinema"))["edit_profile"]
            assert await session.scalar(select(func.count()).select_from(PublicTeam)) == 1

    run(database_url, scenario)


def test_revoked_rights_are_rechecked_in_existing_session(database_url):
    async def scenario(sessions):
        await seed(sessions)
        await add_person(sessions)
        async with sessions.begin() as session:
            await ClubService(session).set_organizer(LEADER, "cinema", MEMBER)
        async with sessions() as stale:
            service = ClubService(stale)
            assert (await service.permissions(MEMBER, "cinema"))["edit_profile"]
            # Keep a reference so this test exercises a live ORM identity-map entry.
            membership = await stale.scalar(
                select(Membership).where(Membership.role == "organizer")
            )
            await stale.commit()
            async with sessions.begin() as session:
                await ClubService(session).remove_organizer(LEADER, "cinema", MEMBER)
            async with stale.begin():
                with pytest.raises(PermissionDenied):
                    await service.update_club(MEMBER, "cinema", "name", "Stale click")
                assert membership.role == "organizer"

    run(database_url, scenario)


def test_rollback_preserves_rights_profile_audit_and_outbox(database_url):
    async def scenario(sessions):
        await seed(sessions)
        await add_person(sessions)
        async with sessions.begin() as session:
            await ClubService(session).set_organizer(LEADER, "cinema", MEMBER)
            before = await session.scalar(select(func.count()).select_from(Audit))
        with pytest.raises(RuntimeError, match="rollback"):
            async with sessions.begin() as session:
                service = ClubService(session)
                await service.remove_organizer(LEADER, "cinema", MEMBER)
                await service.update_club(LEADER, "cinema", "name", "Rolled back")
                raise RuntimeError("rollback")
        async with sessions.begin() as session:
            service = ClubService(session)
            assert (await service.permissions(MEMBER, "cinema"))["edit_profile"]
            assert (await service.get_club("cinema"))["name"] == "Киноклуб"
            assert await session.scalar(select(func.count()).select_from(Audit)) == before
            assert await session.scalar(select(func.count()).select_from(Outbox)) == 1

    run(database_url, scenario)


def test_profile_fields_version_links_and_public_team_survive_restart(database_url):
    async def scenario(sessions):
        await seed(sessions)
        expected = {
            "name": "New cinema",
            "description": "Description",
            "channel": "https://t.me/cinema",
            "cover": "telegram-file-id",
            "links": [{"label": "Site", "url": "https://example.com"}],
            "public_team": [{"name": "Host", "role": "Presenter"}, {"name": "Helper", "role": ""}],
        }
        async with sessions.begin() as session:
            service = ClubService(session)
            for field, value in expected.items():
                await service.update_club(LEADER, "cinema", field, value)
            with pytest.raises(ValidationError, match="Карточка изменилась"):
                await service.update_club(LEADER, "cinema", "name", "Stale", expected_version=1)
            await service.update_club(
                LEADER, "cinema", "public_team", [{"name": "Only", "role": ""}]
            )
        async with sessions.begin() as session:
            card = await ClubService(session).get_club("cinema")
            assert card["version"] == 8
            assert card["public_id"] == "cinema"
            for field, value in expected.items():
                assert card[field] == (
                    [{"name": "Only", "role": ""}] if field == "public_team" else value
                )

    run(database_url, scenario)


def test_invalid_profile_values_and_protected_membership_are_rejected(database_url):
    async def scenario(sessions):
        await seed(sessions)
        async with sessions.begin() as session:
            service = ClubService(session)
            for field, value in [
                ("name", ""),
                ("name", "x" * 201),
                ("level", "IPO"),
                ("channel", "javascript:alert(1)"),
                ("channel", "https://[bad"),
                ("links", [{"label": "X", "url": "file:///etc/passwd"}]),
                ("public_team", [{"name": ""}]),
            ]:
                with pytest.raises(ValidationError):
                    await service.update_club(LEADER, "cinema", field, value)
            with pytest.raises(ValidationError, match="лидера"):
                await service.remove_organizer(ADMIN, "cinema", LEADER)
            with pytest.raises(ValidationError, match="лидера"):
                await service.set_organizer(ADMIN, "cinema", LEADER)
            assert (await service.get_club("cinema"))["version"] == 1

    run(database_url, scenario)


def test_username_never_grants_privileges_and_subscription_is_idempotent(database_url):
    async def scenario(sessions):
        await seed(sessions)
        await add_person(sessions, STRANGER, "@jedi_kitten")
        async with sessions.begin() as session:
            service = ClubService(session)
            assert not await service.is_admin(STRANGER)
            assert not (await service.permissions(STRANGER, "cinema"))["is_leader"]
            assert await service.managed_clubs(STRANGER) == []
            for _ in range(2):
                assert await service.set_subscription(STRANGER, "cinema", True)
            assert await service.get_subscription(STRANGER, "cinema")
            assert await session.scalar(select(func.count()).select_from(Subscription)) == 1
            for _ in range(2):
                assert not await service.set_subscription(STRANGER, "cinema", False)
            assert not await service.get_subscription(STRANGER, "cinema")
            assert await session.scalar(select(func.count()).select_from(Subscription)) == 0

    run(database_url, scenario)


def test_durable_conversation_inbox_and_outbox_constraints(database_url):
    async def scenario(sessions):
        await seed(sessions)
        async with sessions.begin() as session:
            session.add(
                Conversation(
                    telegram_id=LEADER,
                    state="profile.edit",
                    payload={"public_id": "cinema", "field": "description"},
                )
            )
            session.add(TelegramUpdate(update_id=12345, payload={"update_id": 12345}))
        async with sessions.begin() as session:
            conversation = await session.get(Conversation, LEADER)
            assert conversation.state == "profile.edit"
            assert conversation.payload["field"] == "description"
            update = await session.get(TelegramUpdate, 12345)
            assert update.status == "pending" and update.attempts == 0
            message = await session.scalar(select(Outbox))
            dedupe = message.dedupe_key
        with pytest.raises(IntegrityError):
            async with sessions.begin() as session:
                session.add(
                    Outbox(
                        kind="club.created",
                        recipient_telegram_id=LEADER,
                        payload={},
                        dedupe_key=dedupe,
                    )
                )
        with pytest.raises(IntegrityError):
            async with sessions.begin() as session:
                session.add(TelegramUpdate(update_id=12345, payload={}))
        with pytest.raises(IntegrityError):
            async with sessions.begin() as session:
                session.add(TelegramUpdate(update_id=12346, payload={}, status="sent"))
        async with sessions.begin() as session:
            assert await session.scalar(select(func.count()).select_from(Outbox)) == 1
            assert await session.scalar(select(func.count()).select_from(TelegramUpdate)) == 1
            assert await session.scalar(select(func.count()).select_from(TelegramAccount)) == 2

    run(database_url, scenario)


def test_bootstrap_rolls_back_as_one_transaction(database_url):
    async def scenario(sessions):
        with pytest.raises(RuntimeError, match="rollback"):
            async with sessions.begin() as session:
                await ClubService(session).bootstrap([ADMIN], LEADER)
                raise RuntimeError("rollback")
        async with sessions.begin() as session:
            for model in [
                Club,
                ClubLevel,
                GlobalAdmin,
                Membership,
                Outbox,
                Audit,
                Person,
                TelegramAccount,
            ]:
                assert await session.scalar(select(func.count()).select_from(model)) == 0
        await seed(sessions)
        async with sessions.begin() as session:
            assert await ClubService(session).is_admin(ADMIN)
            assert (await ClubService(session).permissions(LEADER, "cinema"))["is_leader"]

    run(database_url, scenario)


def test_bootstrap_overlapping_leader_and_two_admins_preserves_existing_profile(database_url):
    async def scenario(sessions):
        await seed(sessions)
        async with sessions.begin() as session:
            service = ClubService(session)
            await service.update_club(LEADER, "cinema", "description", "User supplied profile")
            await service.bootstrap([ADMIN, LEADER], LEADER)
        async with sessions.begin() as session:
            service = ClubService(session)
            assert await service.is_admin(ADMIN) and await service.is_admin(LEADER)
            assert (await service.permissions(LEADER, "cinema"))["is_leader"]
            assert (await service.get_club("cinema"))["description"] == "User supplied profile"
            for model, count in [(GlobalAdmin, 2), (Person, 2), (Membership, 1), (Outbox, 1)]:
                assert await session.scalar(select(func.count()).select_from(model)) == count

    run(database_url, scenario)
