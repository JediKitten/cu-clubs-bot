"""User-facing temporal formats and a durable, atomic reschedule proposal."""

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from test_events import EDITOR, LEADER, published, seed
from test_events import sessions as sessions

from cu_clubs.datetime_input import MOSCOW, parse_clock, parse_date
from cu_clubs.event_ui import handle_events
from cu_clubs.events import EventService
from cu_clubs.models import Audit, Conversation, Outbox
from cu_clubs.services import PermissionDenied, ValidationError


@pytest.mark.parametrize(
    "raw",
    [
        "14.11.2026",
        "14/11/26",
        "14-11-2026",
        "2026-11-14",
        "14/11",
        "14 ноября",
        "14 ноя 2026",
        "завтра",
    ],
)
def test_date_formats(raw):
    assert parse_date(raw, anchor=date(2026, 1, 1), today=date(2026, 11, 13)) == date(2026, 11, 14)


@pytest.mark.parametrize("raw", ["9:30", "09.30", "9 30", "0930", "930", "09,30"])
def test_clock_formats(raw):
    assert parse_clock(raw).strftime("%H:%M") == "09:30"


@pytest.mark.parametrize("raw", ["24:00", "19:60", "9.3", "random"])
def test_invalid_clocks_are_not_guessed(raw):
    with pytest.raises(ValidationError):
        parse_clock(raw)


async def step(sessions, event_id, *, raw=None, action=None, data=None, actor=LEADER):
    async with sessions.begin() as session:
        state = await session.get(Conversation, actor)
        if state is None:
            state = Conversation(telegram_id=actor, state="idle", payload={})
            session.add(state)
            await session.flush()
        if action:
            data = f"evtime:{state.payload['token']}:{action}"
        screen = await handle_events(
            session,
            actor,
            state,
            SimpleNamespace(text=raw) if raw is not None else None,
            data or "",
            SimpleNamespace(),
        )
        assert screen is not None
        assert all(
            len(b.callback_data.encode()) <= 64
            for row in screen.markup.inline_keyboard
            for b in row
            if b.callback_data
        )
        return screen


async def fixture(sessions):
    await seed(sessions)
    start = datetime(2030, 5, 6, 18, tzinfo=MOSCOW)
    return await published(
        sessions,
        starts_at=start,
        ends_at=start + timedelta(hours=2),
        registration_deadline=start - timedelta(minutes=30),
    )


async def preview(sessions, event_id):
    await step(sessions, event_id, data=f"evf:{event_id}:start")
    await step(sessions, event_id, raw="7/5/2030")
    await step(sessions, event_id, raw="19.30")
    await step(sessions, event_id, action="keep")
    await step(sessions, event_id, raw="2200")
    await step(sessions, event_id, action="keep")
    return await step(sessions, event_id, raw="19")


def test_reschedule_is_atomic_and_back_keeps_the_proposal(sessions):
    async def scenario():
        original = await fixture(sessions)
        public_id = original["public_id"]
        async with sessions() as session:
            before_outbox = await session.scalar(select(func.count()).select_from(Outbox))
        screen = await preview(sessions, public_id)
        assert "Проверь расписание" in screen.text
        async with sessions() as session:
            current = await EventService(session).get_event(public_id, LEADER)
            assert datetime.fromisoformat(current["starts_at"]) == datetime.fromisoformat(
                original["starts_at"]
            )
            assert await session.scalar(select(func.count()).select_from(Outbox)) == before_outbox
        await step(sessions, public_id, action="back")
        invalid = await step(sessions, public_id, raw="1931")
        assert "не позже начала" in invalid.text and "время" in invalid.text
        await step(sessions, public_id, raw="19:00")
        saved = await step(sessions, public_id, action="save")
        assert "Расписание сохранено" in saved.text
        async with sessions() as session:
            changed = await EventService(session).get_event(public_id, LEADER)
            assert changed["version"] == original["version"] + 1
            assert datetime.fromisoformat(changed["starts_at"]).astimezone(MOSCOW) == datetime(
                2030, 5, 7, 19, 30, tzinfo=MOSCOW
            )
            assert datetime.fromisoformat(changed["ends_at"]).astimezone(MOSCOW).hour == 22
            assert (
                datetime.fromisoformat(changed["registration_deadline"]).astimezone(MOSCOW).hour
                == 19
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(Audit).where(Audit.action == "event.updated")
                )
                == 1
            )
            assert (await session.get(Conversation, LEADER)).state == "idle"

    asyncio.run(scenario())


def test_cancel_and_concurrent_change_never_write_the_proposal(sessions):
    async def scenario():
        original = await fixture(sessions)
        public_id = original["public_id"]
        await preview(sessions, public_id)
        await step(sessions, public_id, action="cancel")
        async with sessions() as session:
            assert (await EventService(session).get_event(public_id, LEADER))[
                "version"
            ] == original["version"]
        await preview(sessions, public_id)
        async with sessions.begin() as session:
            await EventService(session).update_event(
                LEADER,
                public_id,
                {"description": "Изменил другой организатор"},
                original["version"],
            )
        screen = await step(sessions, public_id, action="save")
        assert "уже изменили" in screen.text
        async with sessions() as session:
            current = await EventService(session).get_event(public_id, LEADER)
            assert datetime.fromisoformat(current["starts_at"]) == datetime.fromisoformat(
                original["starts_at"]
            )
        with pytest.raises(PermissionDenied):
            await step(sessions, public_id, data=f"evf:{public_id}:start", actor=EDITOR)

    asyncio.run(scenario())


def test_overnight_end_is_explicit_and_invalid_input_stays_in_context(sessions):
    async def scenario():
        original = await fixture(sessions)
        public_id = original["public_id"]
        await step(sessions, public_id, data=f"evf:{public_id}:start")
        invalid = await step(sessions, public_id, raw="31.02.2030")
        assert "Не распознал дату" in invalid.text and "Начало · дата" in invalid.text
        await step(sessions, public_id, raw="7 мая 2030")
        await step(sessions, public_id, raw="23:30")
        await step(sessions, public_id, action="nextday")
        await step(sessions, public_id, raw="01:00")
        await step(sessions, public_id, action="start")
        await step(sessions, public_id, action="save")
        async with sessions() as session:
            changed = await EventService(session).get_event(public_id, LEADER)
            assert datetime.fromisoformat(changed["ends_at"]).astimezone(MOSCOW) == datetime(
                2030, 5, 8, 1, tzinfo=MOSCOW
            )

    asyncio.run(scenario())


def test_wizard_back_traverses_time_fields_without_looping_and_keeps_input(sessions):
    async def scenario():
        await seed(sessions)
        start = datetime(2030, 5, 6, 18, tzinfo=MOSCOW)
        async with sessions.begin() as session:
            event = await EventService(session).create_draft(
                LEADER,
                "cinema",
                {
                    "title": "Тестовая встреча",
                    "starts_at": start,
                    "ends_at": start + timedelta(hours=2),
                    "registration_deadline": start,
                },
            )
            session.add(
                Conversation(
                    telegram_id=LEADER,
                    state="event_edit",
                    payload={
                        "event": event["public_id"],
                        "field": "venue_type",
                        "version": event["version"],
                        "wizard": True,
                        "token": "test-back",
                    },
                )
            )
        public_id = event["public_id"]
        await step(sessions, public_id, data="evprev:test-back")
        await step(sessions, public_id, raw="05.05.2030")
        await step(sessions, public_id, action="back")
        await step(sessions, public_id, action="back")
        await step(sessions, public_id, action="keep")
        async with sessions() as session:
            state = await session.get(Conversation, LEADER)
            assert state.payload["dates"]["registration_deadline"] == "2030-05-05"
        for expected in (4, 3, 2, 1, 0):
            async with sessions() as session:
                assert (await session.get(Conversation, LEADER)).payload["cursor"] == expected
            if expected:
                await step(sessions, public_id, action="back")
        await step(sessions, public_id, raw="8 мая 2030")
        await step(sessions, public_id, raw="19")
        await step(sessions, public_id, action="back")
        await step(sessions, public_id, action="back")
        await step(sessions, public_id, action="previous")
        await step(sessions, public_id, raw="Инструкция после регистрации")
        async with sessions() as session:
            state = await session.get(Conversation, LEADER)
            assert state.state == "event_schedule"
            assert state.payload["dates"]["starts_at"] == "2030-05-08"
            assert state.payload["clocks"]["starts_at"] == "19:00"
            current = await EventService(session).get_event(public_id, LEADER)
            assert datetime.fromisoformat(current["starts_at"]) == start

    asyncio.run(scenario())
