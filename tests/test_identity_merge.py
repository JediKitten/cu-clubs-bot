"""Email verification attaches imported attendance while retaining its history."""

import asyncio

import pytest
from sqlalchemy import select
from test_events import LEADER, STUDENT, published, seed
from test_events import sessions as sessions

from cu_clubs.attendance import AttendanceService
from cu_clubs.attendance_models import Attendance
from cu_clubs.identity import IdentityService, verification_delivery
from cu_clubs.services import ClubService

SECRET = "identity-merge-integration-secret-32"


@pytest.mark.integration
def test_email_verification_merges_imported_attendance_and_keeps_historical_flags(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions)
        async with sessions.begin() as session:
            attendance = AttendanceService(session, SECRET)
            preview = await attendance.preview_import(
                LEADER,
                event["public_id"],
                [
                    {
                        "row": 2,
                        "full_name": "Imported Student",
                        "time_login": "merge.student",
                        "telegram_tag": "@history_tag",
                    }
                ],
            )
            result = await attendance.confirm_import(LEADER, preview["id"], preview["version"])
            assert result["new"] == 1
            imported_attendance = await session.scalar(select(Attendance))
            imported_id = imported_attendance.person_id
            assert imported_attendance.was_bot_registered is False
            assert imported_attendance.source == "import"

        async with sessions.begin() as session:
            challenge = await IdentityService(
                session, SECRET, mail_available=True
            ).start_verification(STUDENT, "Verified Student", "merge.student")
        async with sessions.begin() as session:
            delivery = await verification_delivery(session, challenge["challenge_id"], SECRET)
            verified = await IdentityService(session, SECRET, mail_available=True).verify(
                STUDENT, delivery["code"]
            )
            assert verified["status"] == "verified"
            canonical_id = verified["profile"]["id"]
            assert canonical_id == str(imported_id)
        async with sessions.begin() as session:
            linked = await session.scalar(select(Attendance))
            assert str(linked.person_id) == canonical_id
            assert linked.was_bot_registered is False
            assert linked.source == "import"
            assert linked.telegram_tag_at_mark == "history_tag"
            imported = await ClubService(session)._account(STUDENT)
            assert str(imported.person_id) == canonical_id

    asyncio.run(scenario())
