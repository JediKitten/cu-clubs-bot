"""PostgreSQL integration checks for QR and attendance import behavior."""

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import func, select
from test_events import EDITOR, LEADER, OTHER, STUDENT, published, seed
from test_events import sessions as sessions

from cu_clubs.attendance import AttendanceService
from cu_clubs.attendance_models import Attendance
from cu_clubs.services import PermissionDenied, ValidationError

SECRET = "attendance-integration-secret-32chars"


@pytest.mark.integration
def test_qr_purpose_is_bound_and_attendance_can_exceed_capacity(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions, capacity=1)
        guest_actor = 91991
        async with sessions.begin() as session:
            from cu_clubs.services import ClubService

            await ClubService(session).get_or_create_person(guest_actor, "Guest Nick")
        async with sessions.begin() as session:
            service = AttendanceService(session, SECRET)
            event_qr = await service.event_qr(LEADER, event["public_id"])
            personal_qr = await service.get_personal_qr(STUDENT)
            with pytest.raises(ValidationError):
                await service.mark_event_qr(STUDENT, personal_qr["token"])
            with pytest.raises(ValidationError):
                await service.mark_personal_qr(LEADER, event["public_id"], event_qr["token"])
        async with sessions.begin() as session:
            service = AttendanceService(session, SECRET)
            first = await service.register_guest_and_mark(
                guest_actor, event_qr["token"], "Guest Example"
            )
            again = await service.register_guest_and_mark(
                guest_actor, event_qr["token"], "Guest Example"
            )
            assert first["created"] is True
            assert again["created"] is False
            assert await session.scalar(select(func.count()).select_from(Attendance)) == 1

    asyncio.run(scenario())


@pytest.mark.integration
def test_import_preview_confirm_is_repeatable_and_preserves_source_and_tag(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions)
        async with sessions.begin() as session:
            service = AttendanceService(session, SECRET)
            preview = await service.preview_import(
                LEADER,
                event["public_id"],
                [
                    {
                        "row": 2,
                        "full_name": "Import Person",
                        "time_login": "import.user",
                        "telegram_tag": "@import_user",
                    },
                    {"row": 3, "full_name": "Invalid", "time_login": "bad login"},
                ],
            )
            assert preview["new"] == 1
            assert len(preview["issues"]) == 1
            batch_id, version = preview["id"], preview["version"]
        async with sessions.begin() as session:
            service = AttendanceService(session, SECRET)
            result = await service.confirm_import(LEADER, batch_id, version)
            repeated = await service.confirm_import(LEADER, batch_id, version)
            assert result == repeated
            assert result["new"] == 1 and result["issues"] == 1
        async with sessions.begin() as session:
            service = AttendanceService(session, SECRET)
            imported = await service.mark_student(
                LEADER, event["public_id"], "Import Person", "import.user", "@changed"
            )
            assert imported["created"] is False
            attendance = await session.scalar(select(Attendance))
            assert attendance.source == "import"
            roster = await service.roster(LEADER, event["public_id"])
            assert roster[0]["telegram_tag"] == "import_user"
            with pytest.raises(PermissionDenied):
                await service.roster(EDITOR, event["public_id"])
            with pytest.raises(PermissionDenied):
                await service.roster(OTHER, event["public_id"])
            assert attendance.person_id == UUID(roster[0]["person_id"])

    asyncio.run(scenario())
