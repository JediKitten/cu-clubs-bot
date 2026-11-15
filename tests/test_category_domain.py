"""Temporary category policy acceptance against disposable PostgreSQL schemas."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from test_api_security import api_settings
from test_events import LEADER, STUDENT, published, seed
from test_events import sessions as sessions

from cu_clubs.api_security import ApiError
from cu_clubs.api_service import ApiService
from cu_clubs.attendance import AttendanceService
from cu_clubs.attendance_models import Attendance
from cu_clubs.events import EventService
from cu_clubs.identity import IdentityService
from cu_clubs.models import Person
from cu_clubs.services import ClubService, PermissionDenied, ValidationError

SECRET = "category-domain-synthetic-secret-32chars"
ACTOR = 91881


async def declare(session, category):
    clubs = ClubService(session)
    profile = await clubs.get_or_create_person(ACTOR, "Declared Example")
    identity = IdentityService(session, SECRET, mail_available=False)
    selected = await identity.declare_category(
        ACTOR,
        category,
        **(
            {"full_name": "Declared Example", "time_login": "declared.example"}
            if category in {"student", "staff"}
            else {}
        ),
    )
    assert selected["verified_at"] is None
    assert selected["time_login"] == (
        "declared.example" if category in {"student", "staff"} else None
    )
    assert selected["kind"] is None
    assert selected["category_selected_at"] is not None
    return await session.get(Person, UUID(profile["id"]))


@pytest.mark.parametrize("category", ["student", "staff", "applicant", "external"])
def test_declared_category_registration_and_qr_matrix(sessions, category):
    sessions.configure(info={"test_self_registration_enabled": True})

    async def scenario():
        await seed(sessions)
        event = await published(sessions)
        async with sessions.begin() as session:
            clubs = ClubService(session)
            cinema = await clubs._club("cinema")
            cinema.level_name = "Seed"
            person = await declare(session, category)
            events = EventService(session)
            if category in {"student", "staff"}:
                await events.register(ACTOR, event["public_id"])
            else:
                with pytest.raises(PermissionDenied):
                    await events.register(ACTOR, event["public_id"])
            attendance = AttendanceService(session, SECRET)
            event_qr = await attendance.event_qr(LEADER, event["public_id"])
            personal_qr = await attendance.get_personal_qr(ACTOR)
            first = await attendance.mark_event_qr(ACTOR, event_qr["token"])
            repeat = await attendance.mark_personal_qr(
                LEADER, event["public_id"], personal_qr["token"]
            )
            assert first["created"] and not repeat["created"]
            assert await session.scalar(select(func.count()).select_from(Attendance)) == 1
            mark = await session.scalar(select(Attendance))
            assert mark.category_at_mark == category
            assert mark.guest_at_mark == (category in {"applicant", "external"})
            assert person.verified_at is None
            assert bool(person.time_login) == (category in {"student", "staff"})
            if category in {"applicant", "external"}:
                with pytest.raises(ValidationError):
                    await attendance.mark_manual(LEADER, event["public_id"], person.id)

    asyncio.run(scenario())


@pytest.mark.parametrize("category", ["applicant", "external"])
def test_guest_qr_denied_below_seed(sessions, category):
    sessions.configure(info={"test_self_registration_enabled": True})

    async def scenario():
        await seed(sessions)
        event = await published(sessions)
        async with sessions.begin() as session:
            cinema = await ClubService(session)._club("cinema")
            cinema.level_name = "Pre-Seed"
            await declare(session, category)
            attendance = AttendanceService(session, SECRET)
            qr = await attendance.event_qr(LEADER, event["public_id"])
            personal = await attendance.get_personal_qr(ACTOR)
            with pytest.raises(PermissionDenied):
                await attendance.mark_event_qr(ACTOR, qr["token"])
            with pytest.raises(PermissionDenied):
                await attendance.mark_personal_qr(LEADER, event["public_id"], personal["token"])
            assert await session.scalar(select(func.count()).select_from(Attendance)) == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("category", ["student", "staff"])
def test_api_category_requires_consent_and_rechecks_policy(sessions, category):
    sessions.configure(info={"test_self_registration_enabled": True})

    async def scenario():
        await seed(sessions)
        async with sessions.begin() as session:
            await declare(session, category)
            api = ApiService(session, api_settings())
            credentials = await api.issue_client(
                LEADER, "cinema", "Category client", ["registration:self"]
            )
            client = await api.authenticate(credentials["api_key"])
            link = await api.create_link(client, ACTOR)
            with pytest.raises(ApiError) as pending:
                await api.exchange(client, link["id"], link["exchange_code"])
            assert pending.value.code == "LINK_PENDING"
            view = await api.link_view(ACTOR, link["id"])
            assert view["eligible"] and not view["verified"] and not view["approved"]
            await api.approve_link(ACTOR, link["id"])
            grant = await api.exchange(client, link["id"], link["exchange_code"])
            await session.flush()
            actor, _ = await api.delegated_actor(client, grant["user_token"])
            assert actor == ACTOR
            session.info["test_self_registration_enabled"] = False
            with pytest.raises(ApiError):
                await api.delegated_actor(client, grant["user_token"])
            session.info["test_self_registration_enabled"] = True
            identity = IdentityService(session, SECRET, mail_available=False)
            await identity.declare_category(ACTOR, "external")
            with pytest.raises(ApiError):
                await api.delegated_actor(client, grant["user_token"])

    asyncio.run(scenario())


def test_flag_off_restores_verified_student_rules(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions)
        async with sessions.begin() as session:
            identity = IdentityService(session, SECRET, mail_available=False)
            with pytest.raises(PermissionDenied):
                await identity.declare_category(STUDENT, "staff")
            account = await ClubService(session)._account(STUDENT)
            account.person.declared_category = "staff"
            account.person.category_selected_at = datetime.now(UTC)
            await EventService(session).register(STUDENT, event["public_id"])
            account.person.verified_at = None
            with pytest.raises(PermissionDenied):
                await EventService(session).register(STUDENT, event["public_id"])
            with pytest.raises(PermissionDenied):
                await AttendanceService(session, SECRET).get_personal_qr(STUDENT)

    asyncio.run(scenario())


@pytest.mark.parametrize("category,selected", [("unknown", True), ("student", False), (None, True)])
def test_database_rejects_invalid_or_unpaired_category(sessions, category, selected):
    async def scenario():
        with pytest.raises(IntegrityError):
            async with sessions.begin() as session:
                session.add(
                    Person(
                        full_name="Constraint Example",
                        declared_category=category,
                        category_selected_at=datetime.now(UTC) if selected else None,
                    )
                )
                await session.flush()

    asyncio.run(scenario())


@pytest.mark.parametrize("category", ["student", "staff"])
def test_university_category_requires_valid_identity_without_email_proof(sessions, category):
    sessions.configure(info={"test_self_registration_enabled": True})

    async def scenario():
        async with sessions.begin() as session:
            await ClubService(session).get_or_create_person(ACTOR, "Telegram name")
            identity = IdentityService(session, SECRET, mail_available=False)
            with pytest.raises(ValidationError):
                await identity.declare_category(ACTOR, category)
            with pytest.raises(ValidationError):
                await identity.declare_category(
                    ACTOR, category, full_name="Иван", time_login="bad login"
                )
            selected = await identity.declare_category(
                ACTOR, category, full_name="Иванов  Иван Иванович", time_login=" I.Ivanov "
            )
            assert selected["full_name"] == "Иванов Иван Иванович"
            assert selected["time_login"] == "i.ivanov"
            assert selected["verified_at"] is None

    asyncio.run(scenario())
