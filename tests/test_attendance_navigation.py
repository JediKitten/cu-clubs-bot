"""Back and cancel routes for attendance prompts and confirmations."""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from test_events import LEADER, published, seed
from test_events import sessions as sessions

from cu_clubs.attendance import AttendanceService
from cu_clubs.attendance_models import Attendance
from cu_clubs.attendance_ui import handle_attendance
from cu_clubs.models import Conversation
from cu_clubs.services import ClubService, ValidationError

SECRET = "offline-attendance-navigation-secret-32"


def settings():
    return SimpleNamespace(
        verification_secret=SecretStr(SECRET), telegram_bot_username="offline_test_bot"
    )


def action(screen, prefix):
    return next(
        button.callback_data
        for row in screen.markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith(prefix)
    )


async def step(factory, actor=LEADER, data="", *, raw=None, upload=None):
    async with factory.begin() as session:
        state = await session.get(Conversation, actor)
        if state is None:
            state = Conversation(telegram_id=actor, state="idle", payload={})
            session.add(state)
            await session.flush()
        return await handle_attendance(
            session,
            actor,
            state,
            SimpleNamespace(text=raw) if raw is not None else None,
            data,
            settings(),
            upload=upload,
        )


@pytest.mark.integration
def test_manual_attendance_back_retains_fields_cancel_and_success_routes(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions)
        event_id = event["public_id"]
        await step(sessions, data=f"atmanual:{event_id}")
        login = await step(sessions, raw="Иван Иванов")
        assert "Иван Иванов" in login.text
        invalid = await step(sessions, raw="bad..login")
        assert "логин Тайма" in invalid.text
        async with sessions() as session:
            state = await session.get(Conversation, LEADER)
            assert state.state == "attendance_manual_login"
            assert state.payload["name"] == "Иван Иванов"
        cancelled = await step(sessions, data=action(invalid, "at:"))
        assert "Посещаемость" in cancelled.text

        await step(sessions, data=f"atmanual:{event_id}")
        await step(sessions, raw="Иван Иванов")

        confirm = await step(sessions, raw="ivanov.test")
        login = await step(sessions, data=action(confirm, "atmanualback:"))
        assert "Текущий логин: ivanov.test" in login.text
        confirm = await step(sessions, raw="ivanov.test")
        result = await step(sessions, data=action(confirm, "atmanualconfirm:"))
        assert action(result, "at:") == f"at:{event_id}"
        assert action(result, f"event:{event_id}:link") == f"event:{event_id}:link"
        assert action(result, f"atmanual:{event_id}") == f"atmanual:{event_id}"
        async with sessions() as session:
            marked = await session.scalar(
                select(Attendance).where(Attendance.event_id == event["id"])
            )
            assert marked is not None

    asyncio.run(scenario())


@pytest.mark.integration
def test_guest_qr_back_and_cancel_return_to_granted_original_qr(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions)
        guest = 91991
        async with sessions.begin() as session:
            await ClubService(session).get_or_create_person(guest, "Гость QR")
        async with sessions.begin() as session:
            qr = await AttendanceService(session, SecretStr(SECRET)).event_qr(
                LEADER, event["public_id"]
            )
        eligibility = await step(sessions, guest, data=f"aq:{qr['token']}")
        await step(sessions, guest, data=action(eligibility, "atguest:"))
        confirm = await step(sessions, guest, raw="Гость Примерный")
        name_prompt = await step(sessions, guest, data=action(confirm, "atguestback:"))
        assert "Текущее ФИО: Гость Примерный" in name_prompt.text
        restored = await step(sessions, guest, data=action(name_prompt, "aqi:"))
        assert "Ты студент ЦУ или внешний гость?" in restored.text

    asyncio.run(scenario())


@pytest.mark.integration
def test_import_resolution_preview_back_preserves_resolved_rows(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions)
        actor = LEADER
        await step(sessions, actor, data=f"atupload:{event['public_id']}")
        preview = await step(
            sessions,
            actor,
            upload={
                "rows": [{"row": 2, "full_name": "Другое Имя", "time_login": "u91003"}],
                "issues": [],
            },
        )
        await step(sessions, actor, data=action(preview, "atresolve:"))
        async with sessions() as session:
            state = await session.get(Conversation, actor)
            assert state.state == "attendance_import"
            batch_id = state.payload["batch"]
            old_nonce = state.payload["nonce"]
            old_version = state.payload["version"]
        attendance_screen = await step(sessions, actor, data=f"at:{event['public_id']}")
        preview = await step(sessions, actor, data=action(attendance_screen, "atimportresume:"))
        async with sessions() as session:
            state = await session.get(Conversation, actor)
            assert state.payload["batch"] == batch_id
            assert state.payload["version"] == old_version
            assert state.payload["nonce"] != old_nonce
        with pytest.raises(ValidationError, match="устар"):
            await step(sessions, actor, data=f"atresolve:{old_nonce}:2")

    asyncio.run(scenario())


def test_personal_qr_back_returns_to_the_actual_selection_page():
    async def scenario():
        state = SimpleNamespace(
            state="attendance_person_confirm",
            payload={
                "event": "event-15",
                "qr_id": "00000000-0000-0000-0000-000000000001",
                "name": "Иван Иванов",
                "choices": [
                    {"public_id": f"event-{index}", "title": f"Встреча {index}"}
                    for index in range(32)
                ],
                "page": 1,
                "nonce": "page-token",
            },
        )
        screen = await handle_attendance(
            None, LEADER, state, None, "atpersonback:page-token", settings()
        )
        picks = [
            button.callback_data
            for row in screen.markup.inline_keyboard
            for button in row
            if button.callback_data and button.callback_data.startswith("atpick:")
        ]
        assert len(picks) == 15
        assert picks[0].endswith(":15") and picks[-1].endswith(":29")
        assert state.payload["page"] == 1

        confirm = await handle_attendance(None, LEADER, state, None, picks[0], settings())
        assert "Встреча 15" in confirm.text
        back = await handle_attendance(
            None,
            LEADER,
            state,
            None,
            action(confirm, "atpersonback:"),
            settings(),
        )
        page_again = [
            button.callback_data
            for row in back.markup.inline_keyboard
            for button in row
            if button.callback_data and button.callback_data.startswith("atpick:")
        ]
        assert len(page_again) == 15
        assert page_again[0].endswith(":15") and page_again[-1].endswith(":29")

    asyncio.run(scenario())
