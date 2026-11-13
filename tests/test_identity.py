"""Email verification persists limits, binds one identity, and never trusts a tag."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

from cu_clubs.config import Settings
from cu_clubs.db import make_database
from cu_clubs.identity import AdminService, IdentityService, normalize_login, verification_delivery
from cu_clubs.identity_models import EmailChallenge
from cu_clubs.models import Audit, Membership, Outbox, Person
from cu_clubs.services import ClubService, PermissionDenied, ValidationError

SECRET = "offline-verification-test-secret-32chars"
NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
ADMIN, LEADER, STUDENT, OTHER = 710001, 710002, 710003, 710004


@pytest.mark.parametrize(
    "raw,normalized",
    [
        (" Ivan.Ivanov ", "ivan.ivanov"),
        (" NAME@EDU.CENTRALUNIVERSITY.RU ", "name"),
        ("a", "a"),
    ],
)
def test_login_normalization(raw, normalized):
    assert normalize_login(raw) == normalized


@pytest.mark.parametrize(
    "raw",
    [
        "name@gmail.com",
        "name@@edu.centraluniversity.ru",
        "a b",
        "a..b",
        "_name",
        "name.",
        "яся",
        "@edu.centraluniversity.ru",
        "",
        "a" * 101,
    ],
)
def test_only_university_login_is_accepted(raw):
    with pytest.raises(ValidationError):
        normalize_login(raw)


@pytest.fixture
def sessions():
    configured = os.environ.get("TEST_DATABASE_URL")
    if not configured:
        pytest.skip("Set TEST_DATABASE_URL for PostgreSQL identity checks")
    schema = f"test_identity_{uuid4().hex}"
    engine = create_engine(configured)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    scoped = make_url(configured).update_query_dict({"options": f"-csearch_path={schema}"})
    settings = Settings(
        _env_file=None, database_url=SecretStr(scoped.render_as_string(hide_password=False))
    )
    async_engine, factory = make_database(settings)
    try:
        yield factory
    finally:
        asyncio.run(async_engine.dispose())
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


async def seed(sessions):
    async with sessions.begin() as session:
        service = ClubService(session)
        await service.bootstrap([ADMIN], LEADER)
        await service.get_or_create_person(STUDENT, "Telegram Nick", "jedi_kitten")
        await service.get_or_create_person(OTHER, "Another Nick")


def identity(session, *, now=NOW, mail=True):
    return IdentityService(session, SECRET, mail_available=mail, now=now)


async def verified(sessions, user=STUDENT, login="student", *, now=NOW):
    async with sessions.begin() as session:
        challenge = await identity(session, now=now).start_verification(user, "Иван Иванов", login)
    async with sessions.begin() as session:
        delivery = await verification_delivery(session, challenge["challenge_id"], SECRET, now=now)
        return await identity(session, now=now).verify(user, delivery["code"])


@pytest.mark.integration
def test_challenge_verifies_once_without_plaintext_code_or_username_privilege(sessions):
    async def scenario():
        await seed(sessions)
        async with sessions.begin() as session:
            profile = await identity(session).profile(STUDENT)
            assert profile["verified_at"] is None and profile["kind"] is None
            challenge = await identity(session).start_verification(
                STUDENT, "  Иван   Иванов  ", "STUDENT@EDU.CENTRALUNIVERSITY.RU"
            )
        async with sessions.begin() as session:
            message = await verification_delivery(
                session, challenge["challenge_id"], SECRET, now=NOW
            )
            row = await session.get(EmailChallenge, UUID(challenge["challenge_id"]))
            outbox = await session.scalar(select(Outbox).where(Outbox.kind == "email.verification"))
            assert message["email"] == "student@edu.centraluniversity.ru"
            assert message["code"].isascii() and len(message["code"]) == 6
            assert len(row.code_digest) == 64 and row.code_digest != message["code"]
            assert outbox.payload == {"challenge_id": challenge["challenge_id"]}
            assert message["code"] not in json.dumps(outbox.payload)
            result = await identity(session).verify(STUDENT, message["code"])
            assert result["status"] == "verified"
            assert result["profile"]["full_name"] == "Иван Иванов"
            assert result["profile"]["time_login"] == "student"
            assert result["profile"]["kind"] == "student"
        async with sessions.begin() as session:
            assert (await identity(session).verify(STUDENT, message["code"]))["status"] == "missing"
            assert (
                await verification_delivery(session, challenge["challenge_id"], SECRET, now=NOW)
                is None
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Audit)
                    .where(Audit.action == "identity.email.verified")
                )
                == 1
            )

    asyncio.run(scenario())


@pytest.mark.integration
def test_wrong_attempts_survive_transaction_boundaries_and_exhaust_code(sessions):
    async def scenario():
        await seed(sessions)
        async with sessions.begin() as session:
            challenge = await identity(session).start_verification(
                STUDENT, "Иван Иванов", "student"
            )
        async with sessions.begin() as session:
            message = await verification_delivery(
                session, challenge["challenge_id"], SECRET, now=NOW
            )
        wrong = "111111" if message["code"] != "111111" else "222222"
        for remaining in range(4, -1, -1):
            async with sessions.begin() as session:
                result = await identity(session).verify(STUDENT, wrong)
                assert result["remaining_attempts"] == remaining
                assert result["status"] == ("invalid" if remaining else "exhausted")
        async with sessions.begin() as session:
            assert (await identity(session).verify(STUDENT, message["code"]))[
                "status"
            ] == "exhausted"
            assert (await identity(session).profile(STUDENT))["verified_at"] is None
            assert (
                await verification_delivery(session, challenge["challenge_id"], SECRET, now=NOW)
                is None
            )

    asyncio.run(scenario())


@pytest.mark.integration
def test_resend_invalidates_old_email_and_expiry_prevents_verification(sessions):
    async def scenario():
        await seed(sessions)
        async with sessions.begin() as session:
            first = await identity(session).start_verification(STUDENT, "Иван Иванов", "first")
        async with sessions.begin() as session:
            with pytest.raises(ValidationError, match="минуту"):
                await identity(session).start_verification(STUDENT, "Иван Иванов", "second")
        later = NOW + timedelta(seconds=61)
        async with sessions.begin() as session:
            second = await identity(session, now=later).start_verification(
                STUDENT, "Иван Иванов", "second"
            )
            assert first["challenge_id"] != second["challenge_id"]
        async with sessions.begin() as session:
            assert (
                await verification_delivery(session, first["challenge_id"], SECRET, now=later)
                is None
            )
            delivery = await verification_delivery(
                session, second["challenge_id"], SECRET, now=later
            )
        expired = later + timedelta(minutes=10)
        async with sessions.begin() as session:
            assert (await identity(session, now=expired).verify(STUDENT, delivery["code"]))[
                "status"
            ] == "expired"
            assert (await identity(session).profile(STUDENT))["verified_at"] is None
            assert (
                await verification_delivery(session, second["challenge_id"], SECRET, now=expired)
                is None
            )

    asyncio.run(scenario())


@pytest.mark.integration
def test_missing_mail_does_not_create_challenge_or_implicitly_verify(sessions):
    async def scenario():
        await seed(sessions)
        async with sessions.begin() as session:
            with pytest.raises(ValidationError, match="отправка писем"):
                await identity(session, mail=False).start_verification(
                    STUDENT, "Иван Иванов", "student"
                )
        async with sessions.begin() as session:
            assert await session.scalar(select(func.count()).select_from(EmailChallenge)) == 0
            assert (await identity(session).profile(STUDENT))["verified_at"] is None

    asyncio.run(scenario())


@pytest.mark.integration
def test_two_accounts_cannot_bind_same_email_even_with_both_valid_challenges(sessions):
    async def scenario():
        await seed(sessions)
        async with sessions.begin() as session:
            first = await identity(session).start_verification(STUDENT, "Иван Иванов", "shared")
        later = NOW + timedelta(seconds=61)
        async with sessions.begin() as session:
            second = await identity(session, now=later).start_verification(
                OTHER, "Пётр Петров", "shared"
            )
        async with sessions.begin() as session:
            a = await verification_delivery(session, first["challenge_id"], SECRET, now=later)
            b = await verification_delivery(session, second["challenge_id"], SECRET, now=later)

        async def verify(user, code):
            async with sessions.begin() as session:
                return await identity(session, now=later).verify(user, code)

        results = await asyncio.gather(verify(STUDENT, a["code"]), verify(OTHER, b["code"]))
        assert sorted(result["status"] for result in results) == ["occupied", "verified"]
        async with sessions.begin() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Person)
                    .where(Person.time_login == "shared", Person.verified_at.is_not(None))
                )
                == 1
            )

    asyncio.run(scenario())


@pytest.mark.integration
def test_hourly_mail_limit_cannot_be_bypassed_by_changing_login(sessions):
    async def scenario():
        await seed(sessions)
        for index in range(5):
            async with sessions.begin() as session:
                await identity(session, now=NOW + timedelta(minutes=index * 2)).start_verification(
                    STUDENT, "Иван Иванов", f"student{index}"
                )
        async with sessions.begin() as session:
            with pytest.raises(ValidationError, match="через час"):
                await identity(session, now=NOW + timedelta(minutes=11)).start_verification(
                    STUDENT, "Иван Иванов", "student6"
                )

    asyncio.run(scenario())


@pytest.mark.integration
def test_only_admin_creates_club_with_verified_leader_and_changes_level(sessions):
    async def scenario():
        await seed(sessions)
        async with sessions.begin() as session:
            with pytest.raises(PermissionDenied):
                await AdminService(session).create_club(
                    LEADER, "Литературный клуб", "Seed", STUDENT
                )
        async with sessions.begin() as session:
            with pytest.raises(ValidationError, match="подтвердить"):
                await AdminService(session).create_club(ADMIN, "Литературный клуб", "Seed", STUDENT)
        await verified(sessions)
        async with sessions.begin() as session:
            club = await AdminService(session).create_club(
                ADMIN, "Литературный клуб", "Pre-Seed", STUDENT
            )
            assert club["name"] == "Литературный клуб" and club["level"] == "Pre-Seed"
        async with sessions.begin() as session:
            item = await session.scalar(
                select(Outbox).where(
                    Outbox.recipient_telegram_id == STUDENT, Outbox.kind == "club.created"
                )
            )
            assert item.payload["public_id"] == club["public_id"]
            assert item.payload["level"] == "Pre-Seed"
            with pytest.raises(PermissionDenied):
                await AdminService(session).set_level(STUDENT, club["public_id"], "IPO")
        async with sessions.begin() as session:
            changed = await AdminService(session).set_level(
                ADMIN, club["public_id"], "Seed", expected_version=club["version"]
            )
            assert changed["level"] == "Seed" and changed["version"] == club["version"] + 1
        async with sessions.begin() as session:
            with pytest.raises(ValidationError, match="изменилась"):
                await AdminService(session).set_level(
                    ADMIN, club["public_id"], "IPO", expected_version=club["version"]
                )

    asyncio.run(scenario())


@pytest.mark.integration
def test_atomic_leader_change_keeps_single_leader_and_old_loses_team_management(sessions):
    async def scenario():
        await seed(sessions)
        await verified(sessions)
        await verified(sessions, OTHER, "other")
        async with sessions.begin() as session:
            club = await AdminService(session).create_club(
                ADMIN, "Литературный клуб", "Seed", STUDENT
            )
        async with sessions.begin() as session:
            await AdminService(session).set_leader(
                ADMIN, club["public_id"], OTHER, expected_version=club["version"]
            )
        async with sessions.begin() as session:
            club_service = ClubService(session)
            assert (await club_service.permissions(OTHER, club["public_id"]))["is_leader"]
            assert not (await club_service.permissions(STUDENT, club["public_id"]))["is_leader"]
            assert (
                await session.scalar(
                    select(func.count()).select_from(Membership).where(Membership.role == "leader")
                )
                == 2
            )
            with pytest.raises(PermissionDenied):
                await club_service.set_organizer(STUDENT, club["public_id"], LEADER)
            count = await session.scalar(select(func.count()).select_from(Outbox))
            await AdminService(session).set_leader(ADMIN, club["public_id"], OTHER)
            assert await session.scalar(select(func.count()).select_from(Outbox)) == count

    asyncio.run(scenario())
