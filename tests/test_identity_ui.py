"""Real dialogue transitions exercise the same identity commands and commit limits."""

import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from test_identity import ADMIN, OTHER, SECRET, STUDENT, seed, verified
from test_identity import sessions as sessions

from cu_clubs import legacy
from cu_clubs.identity import verification_delivery
from cu_clubs.identity_models import EmailChallenge
from cu_clubs.identity_ui import handle_identity
from cu_clubs.models import Club, Conversation, Outbox
from cu_clubs.services import ClubService, PermissionDenied, ValidationError


def settings(*, mail=True):
    return SimpleNamespace(
        verification_secret=SecretStr(SECRET),
        smtp_host="smtp.invalid" if mail else None,
        smtp_username="offline-only",
        smtp_password=SecretStr("offline-only"),
        smtp_sender="offline@example.invalid",
    )


def action(screen, prefix):
    for row in screen.markup.inline_keyboard:
        for button in row:
            if button.callback_data and button.callback_data.startswith(prefix):
                return button.callback_data
    raise AssertionError(f"Screen has no {prefix} action")


async def step(sessions, user=STUDENT, *, data="", text=None, mail=True):
    async with sessions.begin() as session:
        state = await session.get(Conversation, user)
        if state is None:
            state = Conversation(telegram_id=user, state="idle", payload={})
            session.add(state)
            await session.flush()
        return await handle_identity(
            session,
            user,
            state,
            SimpleNamespace(text=text) if text is not None else None,
            data,
            settings(mail=mail),
        )


def test_personal_account_keeps_exact_base_and_does_not_claim_verification():
    async def scenario():
        screen = await handle_identity(
            None,
            STUDENT,
            SimpleNamespace(state="idle", payload={}),
            None,
            "profile",
            settings(),
            person={"full_name": "<Иван> & Иванов", "verified_at": None, "kind": None},
        )
        assert screen.text == (
            "<i>Добро пожаловать &lt;Иван&gt; &amp; Иванов, ваша роль "
            "<b>не определена</b></i>\n\nПочта не подтверждена."
        )
        assert screen.banner_role == "personal-account"
        assert action(screen, "auth:") == "auth:profile"

    asyncio.run(scenario())


@pytest.mark.integration
def test_auth_dialogue_commits_wrong_attempt_then_returns_to_original_action(sessions):
    async def scenario():
        await seed(sessions)
        screen = await step(sessions, data="auth:event:original")
        assert screen.text == legacy.text("fio_request") and screen.banner_role == "auth"
        await step(sessions, text="  Иван   Иванов  ")
        preview = await step(sessions, text="STUDENT@EDU.CENTRALUNIVERSITY.RU")
        assert "student@edu.centraluniversity.ru" in preview.text
        pending = await step(sessions, data=action(preview, "authsend"))
        assert "очередь" in pending.text
        async with sessions.begin() as session:
            state = await session.get(Conversation, STUDENT)
            assert state.state == "auth_code"
            challenge_id = state.payload["challenge_id"]
            delivery = await verification_delivery(session, challenge_id, SECRET)
        wrong = "111111" if delivery["code"] != "111111" else "222222"
        invalid = await step(sessions, text=wrong)
        assert invalid.text.startswith(legacy.text("wrong_code"))
        assert "4" in invalid.text
        async with sessions.begin() as session:
            challenge = await session.get(EmailChallenge, UUID(challenge_id))
            assert challenge.attempts == 1
            assert (await ClubService(session)._account(STUDENT)).person.verified_at is None
        success = await step(sessions, text=delivery["code"])
        assert "Почта подтверждена" in success.text
        assert action(success, "event:") == "event:original"
        async with sessions.begin() as session:
            state = await session.get(Conversation, STUDENT)
            assert state.state == "idle" and state.payload == {}
            person = (await ClubService(session)._account(STUDENT)).person
            assert person.full_name == "Иван Иванов" and person.time_login == "student"

    asyncio.run(scenario())


@pytest.mark.integration
def test_auth_send_without_mail_configuration_preserves_preview_and_does_not_send(sessions):
    async def scenario():
        await seed(sessions)
        await step(sessions, data="auth:profile")
        await step(sessions, text="Иван Иванов")
        preview = await step(sessions, text="student")
        with pytest.raises(ValidationError, match="отправка писем"):
            await step(sessions, data=action(preview, "authsend"), mail=False)
        async with sessions.begin() as session:
            state = await session.get(Conversation, STUDENT)
            assert state.state == "auth_preview"
            assert await session.scalar(select(func.count()).select_from(EmailChallenge)) == 0
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Outbox)
                    .where(Outbox.kind == "email.verification")
                )
                == 0
            )

    asyncio.run(scenario())


@pytest.mark.integration
def test_admin_dialogue_rechecks_role_on_every_step(sessions):
    async def scenario():
        await seed(sessions)
        for data in ("adminnew", "adminlevel:cinema", "adminleader:cinema", "adminsave:stale"):
            with pytest.raises(PermissionDenied):
                await step(sessions, data=data)
        async with sessions.begin() as session:
            session.add(Conversation(telegram_id=STUDENT, state="admin_name", payload={}))
        with pytest.raises(PermissionDenied):
            await step(sessions, text="Новый клуб")

    asyncio.run(scenario())


@pytest.mark.integration
def test_old_admin_confirmation_cannot_save_another_current_preview(sessions):
    async def scenario():
        await seed(sessions)
        await step(sessions, ADMIN, data="adminlevel:cinema")
        async with sessions.begin() as session:
            state = await session.get(Conversation, ADMIN)
            assert state.state == "admin_level"
        level_screen = await step(sessions, ADMIN, data="adminlevel:cinema")
        preview = await step(sessions, ADMIN, data=action(level_screen, "adminpick:"))
        old_confirmation = action(preview, "adminsave:")
        new_levels = await step(sessions, ADMIN, data="adminlevel:cinema")
        new_preview = await step(sessions, ADMIN, data=action(new_levels, "adminpick:"))
        new_confirmation = action(new_preview, "adminsave:")
        assert old_confirmation != new_confirmation
        with pytest.raises(ValidationError, match="устар|друг|актуаль"):
            await step(sessions, ADMIN, data=old_confirmation)
        async with sessions.begin() as session:
            assert (await ClubService(session).get_club("cinema"))["level"] == "Seed"
            assert (await session.get(Conversation, ADMIN)).state == "admin_preview"
        await step(sessions, ADMIN, data=new_confirmation)
        async with sessions.begin() as session:
            assert (await ClubService(session).get_club("cinema"))["level"] == "Concept"

    asyncio.run(scenario())


@pytest.mark.integration
def test_admin_creation_uses_verified_leader_and_exact_creation_success(sessions):
    async def scenario():
        await seed(sessions)
        await verified(sessions, OTHER, "leader")
        start = await step(sessions, ADMIN, data="adminnew")
        assert start.text == legacy.text("input_club_name")
        levels = await step(sessions, ADMIN, text="Новый клуб")
        await step(sessions, ADMIN, data=action(levels, "adminpick:"))
        preview = await step(sessions, ADMIN, text=str(OTHER))
        assert "Новый клуб" in preview.text and str(OTHER) in preview.text
        result = await step(sessions, ADMIN, data=action(preview, "adminsave:"))
        assert result.text == legacy.club_created_text("Новый клуб")
        public_id = action(result, "manageclub:").split(":", 1)[1]
        assert public_id != "cinema"
        async with sessions.begin() as session:
            club = await ClubService(session).get_club(public_id)
            assert club["name"] == "Новый клуб" and club["level"] == "Concept"
            assert (await ClubService(session).permissions(OTHER, public_id))["is_leader"]
            assert await session.scalar(select(func.count()).select_from(Club)) == 2

    asyncio.run(scenario())
