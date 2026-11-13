"""Independent acceptance regressions for attendance policy and identity history."""

import asyncio
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from test_events import EDITOR, LEADER, OTHER, PARTNER, STUDENT, fields, published, seed
from test_events import sessions as sessions

from cu_clubs.attendance import AttendanceService
from cu_clubs.attendance_models import Attendance, AttendanceQR
from cu_clubs.event_models import Registration
from cu_clubs.events import EventService
from cu_clubs.models import Club, Person, TelegramAccount
from cu_clubs.services import ClubService, PermissionDenied, ValidationError

SECRET = "independent-attendance-guard-secret-32"


def test_two_qr_paths_exceed_capacity_without_creating_registrations_or_expiring(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions, capacity=1)
        async with sessions.begin() as session:
            service = AttendanceService(session, SECRET)
            event_qr = await service.event_qr(LEADER, event["public_id"])
            personal = await service.get_personal_qr(OTHER)
            student_personal = await service.get_personal_qr(STUDENT)
            for raw in (event_qr["token"], personal["token"]):
                assert len("a_" + raw) <= 64
                assert re.fullmatch(r"[A-Za-z0-9_-]+", "a_" + raw)
            for token in await session.scalars(select(AttendanceQR)):
                token.created_at = datetime.now(UTC) - timedelta(days=30)
        async with sessions.begin() as session:
            service = AttendanceService(session, SECRET)
            first = await service.mark_event_qr(STUDENT, event_qr["token"])
            second = await service.mark_personal_qr(LEADER, event["public_id"], personal["token"])
            repeated = await service.mark_personal_qr(
                LEADER, event["public_id"], student_personal["token"]
            )
            assert first["created"] and second["created"] and not repeated["created"]
            records = list(await session.scalars(select(Attendance)))
            assert len(records) == 2
            assert {record.source for record in records} == {"event_qr", "personal_qr"}
            assert not any(record.was_registered for record in records)
            assert await session.scalar(select(func.count()).select_from(Registration)) == 0

    asyncio.run(scenario())


def test_rotated_qr_and_revoked_organizer_cannot_create_attendance(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions)
        async with sessions.begin() as session:
            clubs = ClubService(session)
            await clubs.set_organizer(
                LEADER,
                "cinema",
                EDITOR,
                edit_profile=False,
                manage_events=False,
                record_attendance=True,
            )
            service = AttendanceService(session, SECRET)
            old = await service.event_qr(EDITOR, event["public_id"])
            await service.event_qr(LEADER, event["public_id"], rotate=True)
            personal = await service.get_personal_qr(STUDENT)
        async with sessions.begin() as session:
            service = AttendanceService(session, SECRET)
            with pytest.raises(ValidationError, match="отозван"):
                await service.mark_event_qr(STUDENT, old["token"])
            await ClubService(session).remove_organizer(LEADER, "cinema", EDITOR)
        async with sessions.begin() as session:
            service = AttendanceService(session, SECRET)
            with pytest.raises(PermissionDenied):
                await service.mark_personal_qr(EDITOR, event["public_id"], personal["token"])
            assert await session.scalar(select(func.count()).select_from(Attendance)) == 0

    asyncio.run(scenario())


def test_guest_requires_qr_and_at_least_one_seed_coorganizer(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions)
        guest = 91989
        async with sessions.begin() as session:
            await ClubService(session).get_or_create_person(guest, "Guest Before")
            cinema = await session.scalar(select(Club).where(Club.public_id == "cinema"))
            cinema.level_name = "Pre-Seed"
            service = AttendanceService(session, SECRET)
            qr = await service.event_qr(LEADER, event["public_id"])
        async with sessions.begin() as session:
            service = AttendanceService(session, SECRET)
            with pytest.raises(PermissionDenied, match="Seed"):
                await service.register_guest_and_mark(guest, qr["token"], "Guest Example")
            assert await session.scalar(select(func.count()).select_from(Attendance)) == 0
        async with sessions.begin() as session:
            events = EventService(session)
            draft = await events.create_draft(LEADER, "cinema", fields())
            invitation = await events.invite_partner(
                LEADER, draft["public_id"], "p" * 32, draft["version"]
            )
            joint = await events.respond_invitation(PARTNER, invitation["invitation_id"], True)
            qr = await AttendanceService(session, SECRET).event_qr(LEADER, joint["public_id"])
        async with sessions.begin() as session:
            service = AttendanceService(session, SECRET)
            result = await service.register_guest_and_mark(guest, qr["token"], "Guest Example")
            assert result["created"]
            record = await session.scalar(select(Attendance))
            assert record.guest_at_mark and record.source == "event_qr"
            assert await session.scalar(select(func.count()).select_from(Registration)) == 0
            with pytest.raises(ValidationError):
                await service.mark_manual(LEADER, joint["public_id"], record.person_id)

    asyncio.run(scenario())


def test_import_fio_conflict_needs_explicit_review_without_verifying_or_renaming(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions)
        async with sessions.begin() as session:
            person = Person(
                full_name="Canonical Student", time_login="guard.import", kind="student"
            )
            session.add(person)
            await session.flush()
            person_id = person.id
            service = AttendanceService(session, SECRET)
            preview = await service.preview_import(
                LEADER,
                event["public_id"],
                [
                    {
                        "row": 2,
                        "full_name": "Different Student",
                        "time_login": "guard.import",
                        "telegram_tag": "@reference_tag",
                    }
                ],
            )
            assert preview["new"] == 0 and len(preview["conflicts"]) == 1
            assert await session.scalar(select(func.count()).select_from(Attendance)) == 0
        async with sessions.begin() as session:
            service = AttendanceService(session, SECRET)
            resolved = await service.resolve_import(LEADER, preview["id"], [2], preview["version"])
            assert resolved["new"] == 1 and resolved["conflicts"] == []
            result = await service.confirm_import(LEADER, preview["id"], resolved["version"])
            assert result["new"] == 1 and result["skipped"] == 0
            assert (
                await service.confirm_import(LEADER, preview["id"], resolved["version"]) == result
            )
        async with sessions.begin() as session:
            person = await session.get(Person, person_id)
            record = await session.scalar(
                select(Attendance).where(Attendance.person_id == person_id)
            )
            assert person.full_name == "Canonical Student" and person.verified_at is None
            assert record.source == "import" and not record.was_bot_registered
            assert record.telegram_tag_at_mark == "reference_tag"
            assert not await session.scalar(
                select(TelegramAccount).where(TelegramAccount.person_id == person_id)
            )
            assert await session.scalar(select(func.count()).select_from(Attendance)) == 1

    asyncio.run(scenario())
