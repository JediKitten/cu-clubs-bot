"""Meeting acceptance checks using real PostgreSQL and isolated schemas."""

import asyncio
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

from cu_clubs import events as event_module
from cu_clubs.config import Settings
from cu_clubs.db import make_database
from cu_clubs.event_models import Event, EventClub, EventInvitation, RegistrationHistory
from cu_clubs.events import EventNotFound, EventService, validate_fields
from cu_clubs.models import Club, Membership, Outbox, Person
from cu_clubs.services import ClubService, PermissionDenied, ValidationError

ADMIN, LEADER, STUDENT, OTHER, PARTNER, EDITOR = range(91001, 91007)


@pytest.fixture
def sessions():
    configured = os.environ.get("TEST_DATABASE_URL")
    if not configured:
        pytest.skip("Set TEST_DATABASE_URL for real PostgreSQL event checks")
    schema = f"test_events_{uuid4().hex}"
    engine = create_engine(configured)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    scoped = make_url(configured).update_query_dict({"options": f"-csearch_path={schema}"})
    async_engine, factory = make_database(
        Settings(
            _env_file=None, database_url=SecretStr(scoped.render_as_string(hide_password=False))
        )
    )
    try:
        yield factory
    finally:
        asyncio.run(async_engine.dispose())
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


async def seed(factory):
    async with factory.begin() as session:
        clubs = ClubService(session)
        await clubs.bootstrap([ADMIN], LEADER)
        for actor in (STUDENT, OTHER, PARTNER, EDITOR):
            account = await clubs.get_or_create_person(actor, f"Student {actor}")
            person = await session.get(Person, UUID(account["id"]))
            person.kind, person.verified_at, person.time_login = (
                "student",
                datetime.now(UTC),
                f"u{actor}",
            )
        partner = Club(public_id="p" * 32, name="Второй клуб", level_name="Seed")
        session.add(partner)
        await session.flush()
        partner_account = await clubs._account(PARTNER)
        session.add(
            Membership(club_id=partner.id, person_id=partner_account.person_id, role="leader")
        )
        editor = await clubs._account(EDITOR)
        cinema = await clubs._club("cinema")
        session.add(
            Membership(
                club_id=cinema.id,
                person_id=editor.person_id,
                role="organizer",
                edit_profile=False,
                manage_events=True,
                record_attendance=False,
            )
        )


def fields(**overrides):
    start = datetime.now(UTC) + timedelta(days=2)
    value = {
        "title": "Встреча киноклуба",
        "description": "Смотрим и обсуждаем фильм.",
        "starts_at": start,
        "ends_at": start + timedelta(hours=2),
        "capacity": 2,
        "post_registration_text": "Секрет для записавшихся",
    }
    value.update(overrides)
    return value


async def published(factory, **overrides):
    async with factory.begin() as session:
        service = EventService(session)
        event = await service.create_draft(LEADER, "cinema", fields(**overrides))
        return await service.publish(LEADER, event["public_id"], event["version"])


@pytest.mark.parametrize(
    "bad",
    [
        {"capacity": True},
        {"capacity": 0},
        {"capacity": -1},
        {"expected_attendance": -1},
        {"needs_talk": "yes"},
        {"visibility": "secret"},
        {"unknown": 1},
        {"starts_at": datetime(2030, 1, 1)},
        {"title": "abc"},
        {"title": "x" * 46},
        {"description": "x" * 251},
        {"post_registration_text": "short"},
    ],
)
def test_invalid_event_input_is_rejected_without_a_database(bad):
    with pytest.raises(ValidationError):
        validate_fields(bad)


def test_calendar_and_registration_lifecycle_has_no_duplicates(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions)
        async with sessions.begin() as session:
            service = EventService(session)
            public = await service.public_events(
                datetime.now(UTC), datetime.now(UTC) + timedelta(days=7)
            )
            assert len(public) == 1 and public[0]["public_id"] == event["public_id"]
            first = await service.register(STUDENT, event["public_id"])
            second = await service.register(STUDENT, event["public_id"])
            assert first["registered_count"] == second["registered_count"] == 1
            assert len(await service.my_events(STUDENT)) == 1
            await service.cancel_registration(STUDENT, event["public_id"])
            await service.cancel_registration(STUDENT, event["public_id"])
            again = await service.register(STUDENT, event["public_id"])
            assert again["registered_count"] == 1
            assert await session.scalar(select(func.count()).select_from(RegistrationHistory)) == 3

    asyncio.run(scenario())


def test_competing_registration_gets_exactly_one_last_place(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions, capacity=1)

        async def register(actor):
            try:
                async with sessions.begin() as session:
                    await EventService(session).register(actor, event["public_id"])
                return "registered"
            except ValidationError as error:
                return str(error)

        results = await asyncio.gather(register(STUDENT), register(OTHER))
        assert sorted(results) == sorted(["registered", "Все места заняты."])

    asyncio.run(scenario())


def test_closed_draft_and_cancelled_visibility_preserve_capability(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions, visibility="closed")
        async with sessions.begin() as session:
            service = EventService(session)
            assert (
                await service.public_events(
                    datetime.now(UTC), datetime.now(UTC) + timedelta(days=7)
                )
                == []
            )
            with pytest.raises(EventNotFound):
                await service.get_event(event["public_id"], STUDENT)
            await service.get_event(event["public_id"], STUDENT, via_link=True)
            registered = await service.register(STUDENT, event["public_id"], via_link=True)
            assert registered["registered"]
            assert (await service.get_event(event["public_id"], STUDENT))["registered"]
            await service.cancel_event(LEADER, event["public_id"], event["version"])
            assert (await service.get_event(event["public_id"], OTHER, via_link=True))[
                "status"
            ] == "cancelled"
            with pytest.raises(EventNotFound):
                await service.get_event(event["public_id"], OTHER)
            with pytest.raises(ValidationError, match="Регистрация завершена"):
                await service.register(OTHER, event["public_id"], via_link=True)

    asyncio.run(scenario())


def test_registration_requires_verified_student_and_management_is_revocable(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions)
        async with sessions.begin() as session:
            account = await ClubService(session)._account(OTHER)
            account.person.verified_at = None
        async with sessions.begin() as session:
            with pytest.raises(PermissionDenied, match="почту"):
                await EventService(session).register(OTHER, event["public_id"])
            service = EventService(session)
            updated = await service.update_event(
                EDITOR, event["public_id"], {"room": "305"}, event["version"]
            )
            with pytest.raises(ValidationError, match="уже изменил"):
                await service.update_event(
                    LEADER, event["public_id"], {"room": "306"}, event["version"]
                )
            with pytest.raises(PermissionDenied):
                await service.update_event(
                    EDITOR,
                    event["public_id"],
                    {"starts_at": datetime.now(UTC) + timedelta(days=3)},
                    updated["version"],
                )
        async with sessions.begin() as session:
            await ClubService(session).remove_organizer(LEADER, "cinema", EDITOR)
        async with sessions.begin() as session:
            with pytest.raises(PermissionDenied):
                await EventService(session).update_event(
                    EDITOR, event["public_id"], {"room": "306"}, updated["version"]
                )

    asyncio.run(scenario())


def test_capacity_cannot_be_reduced_below_active_registrations(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions)
        async with sessions.begin() as session:
            service = EventService(session)
            await service.register(STUDENT, event["public_id"])
            await service.register(OTHER, event["public_id"])
            with pytest.raises(ValidationError, match="ниже"):
                await service.update_event(
                    LEADER, event["public_id"], {"capacity": 1}, event["version"]
                )
            assert (await service.get_event(event["public_id"], LEADER))["capacity"] == 2

    asyncio.run(scenario())


def test_late_cancel_and_reschedule_preserve_original_start_population(sessions, monkeypatch):
    async def scenario():
        await seed(sessions)
        event = await published(sessions)
        async with sessions.begin() as session:
            await EventService(session).register(STUDENT, event["public_id"])
        later = datetime.fromisoformat(event["starts_at"]) + timedelta(minutes=5)
        monkeypatch.setattr(event_module, "utcnow", lambda: later)
        async with sessions.begin() as session:
            service = EventService(session)
            await service.cancel_registration(STUDENT, event["public_id"])
            new_start = later + timedelta(days=1)
            result = await service.update_event(
                LEADER,
                event["public_id"],
                {
                    "starts_at": new_start,
                    "ends_at": new_start + timedelta(hours=2),
                    "registration_deadline": new_start,
                },
                event["version"],
            )
            account = await ClubService(session)._account(STUDENT)
            assert result["start_registration_snapshot"] == [str(account.person_id)]
            assert result["public_id"] == event["public_id"]
            assert not result["registered"]
            model = await session.get(Event, UUID(event["id"]))
            assert model.snapshot_starts_at == datetime.fromisoformat(event["starts_at"])

    asyncio.run(scenario())


def test_collaboration_requires_partner_leader_and_current_version(sessions):
    async def scenario():
        await seed(sessions)
        async with sessions.begin() as session:
            service = EventService(session)
            draft = await service.create_draft(LEADER, "cinema", fields())
            pending = await service.invite_partner(
                LEADER, draft["public_id"], "p" * 32, draft["version"]
            )
            assert not (await service.pending_invitations(PARTNER))[0]["can_manage"]
            assert (await service.pending_invitations(PARTNER))[0]["can_review"]
            first_id = pending["invitation_id"]
            changed = await service.update_draft(
                LEADER, draft["public_id"], {"room": "310"}, pending["version"]
            )
            assert changed["status"] == "pending_partner"
            with pytest.raises(ValidationError, match="устарело"):
                await service.respond_invitation(PARTNER, first_id, True)
            current = (await service.pending_invitations(PARTNER))[0]
            with pytest.raises(PermissionDenied, match="лидер"):
                await service.respond_invitation(ADMIN, current["invitation_id"], True)
            accepted = await service.respond_invitation(PARTNER, current["invitation_id"], True)
            assert accepted["status"] == "published" and accepted["can_manage"]
            assert len(accepted["clubs"]) == 2
            await service.respond_invitation(PARTNER, current["invitation_id"], True)
            assert await session.scalar(select(func.count()).select_from(EventClub)) == 2
            updated = await service.update_event(
                PARTNER, accepted["public_id"], {"room": "311"}, accepted["version"]
            )
            assert updated["room"] == "311"
            assert await session.scalar(select(func.count()).select_from(EventInvitation)) == 2
            notifications = list(
                await session.scalars(select(Outbox).where(Outbox.kind == "event.published"))
            )
            assert {row.recipient_telegram_id for row in notifications} == {LEADER, PARTNER}

    asyncio.run(scenario())


def test_old_cancel_confirmation_cannot_cancel_a_new_registration(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions)
        async with sessions.begin() as session:
            first = await EventService(session).register(STUDENT, event["public_id"])
        async with sessions.begin() as session:
            service = EventService(session)
            await service.cancel_registration(STUDENT, event["public_id"])
            await service.register(STUDENT, event["public_id"])
            with pytest.raises(ValidationError, match="Запись изменилась"):
                await service.cancel_registration(
                    STUDENT, event["public_id"], expected_registered_at=first["registered_at"]
                )
            assert (await service.get_event(event["public_id"], STUDENT))["registered"]

    asyncio.run(scenario())


def test_template_copies_settings_but_not_time_room_or_participants(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions, room="305", needs_talk=True)
        async with sessions.begin() as session:
            service = EventService(session)
            template = await service.save_template(LEADER, event["public_id"], "Показ")
            result = await service.from_template(LEADER, template["public_id"])
            assert result["status"] == "draft" and result["needs_talk"]
            assert result["room"] is None and result["starts_at"] is None
            assert result["registered_count"] == 0 and len(result["clubs"]) == 1
            assert result["public_id"] != event["public_id"]
            await service.delete_template(LEADER, template["public_id"], template["version"])
            assert await service.templates(LEADER, "cinema") == []

    asyncio.run(scenario())


def test_event_ui_wizard_collaboration_and_registration_buttons_fit_telegram(sessions):
    from types import SimpleNamespace

    from cu_clubs.event_ui import handle_events

    async def scenario():
        await seed(sessions)
        state = SimpleNamespace(state="idle", payload={})
        settings = SimpleNamespace(telegram_bot_username="offline_test_bot")

        async def route(data="", raw=None, actor=LEADER):
            async with sessions.begin() as session:
                message = SimpleNamespace(text=raw) if raw is not None else None
                screen = await handle_events(session, actor, state, message, data, settings)
                assert screen is not None
                if screen.markup:
                    assert all(
                        len(button.callback_data.encode()) <= 64
                        for row in screen.markup.inline_keyboard
                        for button in row
                    )
                return screen

        def button(screen, prefix):
            return next(
                button.callback_data
                for row in screen.markup.inline_keyboard
                for button in row
                if button.callback_data.startswith(prefix)
            )

        await route("eventnew:cinema")
        await route("evblank:cinema")
        public_id = state.payload["event"]
        await route(raw="Показ киноклуба")
        await route(raw="Смотрим фильм и обсуждаем.")
        screen = await route(raw="Важные инструкции после записи")
        assert "Начало · дата" in screen.text
        start = datetime.now(UTC) + timedelta(days=2)
        await route(raw=start.strftime("%d.%m.%Y %H:%M"))
        await route(raw=(start + timedelta(hours=2)).strftime("%d.%m.%Y %H:%M"))
        await route(f"evtime:{state.payload['token']}:start")
        await route(f"evtime:{state.payload['token']}:save")
        await route(f"evchoice:{state.payload['token']}:campus")
        await route(raw="305")
        await route(f"evchoice:{state.payload['token']}:skip")
        await route(raw="5")
        await route(raw="10")
        await route(f"evchoice:{state.payload['token']}:no")
        await route(f"evchoice:{state.payload['token']}:yes")
        screen = await route(f"evchoice:{state.payload['token']}:open")
        assert state.state == "idle"
        assert "Черновик" in screen.text
        screen = await route(f"evpartner:{public_id}")
        invitation_button = button(screen, "evinvite:")
        assert len(invitation_button) < 40
        await route(invitation_button)
        with pytest.raises(ValidationError, match="устарела"):
            await route(invitation_button)
        async with sessions.begin() as session:
            invitation = (await EventService(session).pending_invitations(PARTNER))[0]
        await route(f"evreview:{invitation['invitation_id']}", actor=PARTNER)
        await route(f"evaccept:{invitation['invitation_id']}", actor=PARTNER)
        screen = await route(f"event:{public_id}", actor=STUDENT)
        assert "Важные инструкции" not in screen.text
        screen = await route(f"evreg:{public_id}", actor=STUDENT)
        assert "Важные инструкции" in screen.text
        screen = await route(f"evunregask:{public_id}", actor=STUDENT)
        old_cancel = button(screen, "evunreg:")
        await route(old_cancel, actor=STUDENT)
        await route(f"evreg:{public_id}", actor=STUDENT)
        with pytest.raises(ValidationError, match="устарела"):
            await route(old_cancel, actor=STUDENT)
        await route("future:events", actor=STUDENT)
        await route("future:mine", actor=STUDENT)

    asyncio.run(scenario())
