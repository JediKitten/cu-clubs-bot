"""Navigation contracts for identity verification prompts."""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from test_events import LEADER, OTHER, published
from test_events import STUDENT as EVENT_STUDENT
from test_events import seed as seed_events
from test_identity import STUDENT, seed
from test_identity import sessions as sessions

from cu_clubs.attendance import AttendanceService
from cu_clubs.attendance_ui import handle_attendance
from cu_clubs.identity_ui import _origin, handle_identity
from cu_clubs.models import Conversation
from cu_clubs.services import ClubService

SECRET = "offline-auth-navigation-secret-32"


def settings(*, mail=True):
    return SimpleNamespace(
        verification_secret=SecretStr(SECRET),
        smtp_host="smtp.invalid" if mail else None,
        smtp_username="offline-only" if mail else None,
        smtp_password=SecretStr("offline-only") if mail else None,
        smtp_sender="offline@example.invalid" if mail else None,
    )


def action(screen, prefix):
    return next(
        button.callback_data
        for row in screen.markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith(prefix)
    )


async def step(factory, data="", *, raw=None):
    async with factory.begin() as session:
        state = await session.get(Conversation, STUDENT)
        if state is None:
            state = Conversation(telegram_id=STUDENT, state="idle", payload={})
            session.add(state)
            await session.flush()
        return await handle_identity(
            session,
            STUDENT,
            state,
            SimpleNamespace(text=raw) if raw is not None else None,
            data,
            settings(),
        )


def test_auth_back_chain_retains_fio_and_login(sessions):
    async def scenario():
        await seed(sessions)
        fio = await step(sessions, "auth:profile")
        login = await step(sessions, raw="Иван Иванов")
        assert "Иван Иванов" in login.text
        preview = await step(sessions, raw="student.nav")
        assert "student.nav@edu.centraluniversity.ru" in preview.text

        login = await step(sessions, action(preview, "authback:"))
        assert "Текущий логин: student.nav" in login.text
        fio = await step(sessions, action(login, "authback:"))
        assert "Текущее ФИО: Иван Иванов" in fio.text

        async with sessions() as session:
            state = await session.get(Conversation, STUDENT)
            assert state.state == "auth_fio"
            assert state.payload["full_name"] == "Иван Иванов"
            assert state.payload["login"] == "student.nav"

    asyncio.run(scenario())


def test_auth_cancel_returns_to_profile_and_clears_auth_state(sessions):
    async def scenario():
        await seed(sessions)
        fio = await step(sessions, "auth:profile")
        result = await step(sessions, action(fio, "authcancel:"))
        assert "Почта не подтверждена" in result.text
        async with sessions() as session:
            state = await session.get(Conversation, STUDENT)
            assert state.state == "idle" and state.payload == {}

    asyncio.run(scenario())


def test_auth_origin_is_strictly_whitelisted():
    assert _origin("profile") == "profile"
    assert _origin("aqpersonal") == "aqpersonal"
    assert _origin("event:abc_123:link") == "event:abc_123:link"
    assert _origin("aqi:12345678-1234-1234-1234-123456789abc") == (
        "aqi:12345678-1234-1234-1234-123456789abc"
    )
    assert _origin("apilink:0123456789abcdef0123456789abcdef") == (
        "apilink:0123456789abcdef0123456789abcdef"
    )
    for unsafe in ("menu", "event:bad/id", "aqi:not-a-grant", "apilink:xyz", "admin"):
        assert _origin(unsafe) == "profile"


@pytest.mark.integration
def test_qr_auth_cancel_resumes_valid_grant_and_falls_back_for_revoked_grant(
    sessions,
):
    async def scenario():
        await seed_events(sessions)
        event = await published(sessions)
        settings_value = settings()
        for actor in (OTHER, EVENT_STUDENT):
            async with sessions.begin() as session:
                account = await ClubService(session)._account(actor)
                account.person.kind = None
                account.person.verified_at = None
                qr = await AttendanceService(session, SecretStr(SECRET)).event_qr(
                    LEADER, event["public_id"]
                )
                state = await session.get(Conversation, actor)
                if state is None:
                    state = Conversation(telegram_id=actor, state="idle", payload={})
                    session.add(state)
                    await session.flush()
                eligibility = await handle_attendance(
                    session, actor, state, None, f"aq:{qr['token']}", settings_value
                )
                fio = await handle_identity(
                    session,
                    actor,
                    state,
                    None,
                    action(eligibility, "auth:"),
                    settings_value,
                )
                cancel = action(fio, "authcancel:")

            if actor == EVENT_STUDENT:
                async with sessions.begin() as session:
                    await AttendanceService(session, SecretStr(SECRET)).revoke_qr(
                        LEADER, qr["token"]
                    )
            async with sessions.begin() as session:
                state = await session.get(Conversation, actor)
                returned = await handle_identity(
                    session, actor, state, None, cancel, settings_value
                )
                if actor == OTHER:
                    assert "Ты студент ЦУ или внешний гость?" in returned.text
                else:
                    assert "Почта не подтверждена" in returned.text
                    assert "Продолжить" not in str(returned.markup)

    asyncio.run(scenario())
