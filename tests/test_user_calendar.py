"""Calendar filtering/privacy, real PostgreSQL access and parseable ICS contracts."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from aiogram.methods import SendDocument
from icalendar import Calendar
from test_events import LEADER, OTHER, PARTNER, STUDENT, fields, published, seed
from test_events import sessions as sessions
from test_expanded_dispatch import settings, shown, transport

from cu_clubs import legacy
from cu_clubs.api import EventFields, event_dto
from cu_clubs.attendance import AttendanceService
from cu_clubs.event_models import Event
from cu_clubs.event_ui import event_text, handle_events
from cu_clubs.events import EventNotFound, EventService, validate_fields
from cu_clubs.models import Conversation
from cu_clubs.services import ValidationError
from cu_clubs.template_ui import template_text
from cu_clubs.user_calendar import UserCalendarService, event_ics, week_bounds


def buttons(screen):
    return [button for row in screen.markup.inline_keyboard for button in row]


async def step(factory, data, *, actor=STUDENT, raw=None):
    async with factory.begin() as session:
        state = await session.get(Conversation, actor)
        if state is None:
            state = Conversation(telegram_id=actor, state="idle", payload={})
            session.add(state)
            await session.flush()
        screen = await handle_events(
            session,
            actor,
            state,
            SimpleNamespace(text=raw) if raw is not None else None,
            data,
            SimpleNamespace(),
        )
        for button in buttons(screen):
            assert len(button.callback_data.encode()) <= 64
        return screen


def test_moscow_week_across_new_year_and_utc_boundary():
    start, end = week_bounds(0, datetime(2026, 12, 27, 22, 15, tzinfo=UTC))
    assert start.isoformat() == "2026-12-28T00:00:00+03:00"
    assert end.isoformat() == "2027-01-04T00:00:00+03:00"
    assert week_bounds(-1, start)[1] == start
    for invalid in (True, "1", 261, -261):
        with pytest.raises(ValidationError):
            week_bounds(invalid)


@pytest.mark.parametrize("contact", [None, 17, "x" * 301, "name\x00private"])
def test_invalid_public_contact_rejected_by_shared_validation(contact):
    with pytest.raises(ValidationError):
        validate_fields({"public_contact": contact})


def test_ics_unicode_escaping_status_stable_uid_and_private_field_allowlist():
    card = {
        "public_id": "a" * 32,
        "title": "Кино; книги, музыка\nBEGIN:VEVENT",
        "description": "Обсуждение\\фильма\nПо-русски",
        "starts_at": "2030-01-02T18:00:00+03:00",
        "ends_at": "2030-01-02T20:00:00+03:00",
        "updated_at": "2029-12-25T00:00:00+00:00",
        "room": "B714",
        "clubs": [{"name": "Киноклуб"}],
        "public_contact": "@responsible",
        "visibility": "closed",
        "status": "cancelled",
        "version": 7,
        "post_registration_text": "SUPER_SECRET",
        "author_person_id": "PRIVATE_PERSON",
        "last_editor_telegram_id": 111222333,
        "url": "PRIVATE_LINK",
    }
    payload = event_ics(card)
    parsed = Calendar.from_ical(payload)
    components = parsed.walk("VEVENT")
    assert len(components) == 1
    event = components[0]
    assert str(event["SUMMARY"]) == card["title"]
    assert event.decoded("DTSTART") == datetime(2030, 1, 2, 15, tzinfo=UTC)
    assert str(event["CLASS"]) == "PRIVATE" and str(event["STATUS"]) == "CANCELLED"
    assert event["SEQUENCE"] == 7 and str(event["UID"]) == "a" * 32 + "@cu-clubs"
    assert "@responsible" in str(event["DESCRIPTION"])
    for private in (b"SUPER_SECRET", b"PRIVATE_PERSON", b"111222333", b"PRIVATE_LINK"):
        assert private not in payload
    card.update(version=8, room="B710")
    changed = Calendar.from_ical(event_ics(card)).walk("VEVENT")[0]
    assert changed["UID"] == event["UID"] and changed["SEQUENCE"] == 8
    assert str(changed["LOCATION"]) == "B710"


def test_week_filters_partners_without_closed_drafts_or_duplicate_events(sessions):
    async def scenario():
        await seed(sessions)
        start, end = week_bounds(1)
        public = await published(
            sessions, starts_at=start + timedelta(hours=12), ends_at=start + timedelta(hours=13)
        )
        await published(
            sessions,
            title="Только по приглашению",
            visibility="closed",
            starts_at=start + timedelta(days=1),
            ends_at=start + timedelta(days=1, hours=1),
        )
        async with sessions.begin() as session:
            service = EventService(session)
            event = await service.create_draft(
                LEADER,
                "cinema",
                fields(
                    starts_at=start + timedelta(days=2), ends_at=start + timedelta(days=2, hours=1)
                ),
            )
            invite = await service.invite_partner(
                LEADER, event["public_id"], "p" * 32, event["version"]
            )
            await service.respond_invitation(PARTNER, invite["invitation_id"], True)
            draft = await service.create_draft(
                LEADER,
                "cinema",
                fields(
                    starts_at=start + timedelta(days=3), ends_at=start + timedelta(days=3, hours=1)
                ),
            )
            week = await UserCalendarService(session).week(1)
            assert {club["public_id"] for club in week["clubs"]} == {"cinema", "p" * 32}
            assert {e["public_id"] for e in week["events"]} == {
                public["public_id"],
                event["public_id"],
            }
            assert draft["public_id"] not in {e["public_id"] for e in week["events"]}
            partner = await UserCalendarService(session).week(1, "p" * 32)
            assert [e["public_id"] for e in partner["events"]] == [event["public_id"]]
            assert (await UserCalendarService(session).week(1, "invented"))["events"] == []
            # End boundary belongs only to the next week.
            boundary = await service.create_draft(
                LEADER, "cinema", fields(starts_at=end, ends_at=end + timedelta(hours=1))
            )
            await service.publish(LEADER, boundary["public_id"], boundary["version"])
            assert len((await UserCalendarService(session).week(1))["events"]) == 2

    asyncio.run(scenario())


def test_personal_history_and_ics_recheck_closed_access_and_imported_visits(sessions):
    async def scenario():
        await seed(sessions)
        future = await published(sessions, visibility="closed", public_contact="@only_public")
        past = await published(sessions, visibility="closed")
        cancelled = await published(sessions)
        async with sessions.begin() as session:
            service = UserCalendarService(session)
            await service.register(STUDENT, future["public_id"], via_link=True)
            await service.register(STUDENT, cancelled["public_id"])
            await service.cancel_event(LEADER, cancelled["public_id"], cancelled["version"])
            with pytest.raises(EventNotFound):
                await service.export(OTHER, future["public_id"])
            assert b"@only_public" in await service.export(STUDENT, future["public_id"])
            # A real visit without prior registration appears in history.
            await AttendanceService(session, "test-secret-32-bytes-calendar").mark_student(
                LEADER, past["public_id"], f"Student {STUDENT}", f"u{STUDENT}"
            )
            row = await session.get(Event, UUID(past["id"]))
            row.starts_at, row.ends_at = (
                datetime.now(UTC) - timedelta(hours=2),
                datetime.now(UTC) - timedelta(hours=1),
            )
            row.registration_deadline = row.starts_at
            await session.flush()
            assert [e["public_id"] for e in await service.mine(STUDENT)] == [future["public_id"]]
            assert {e["public_id"] for e in await service.mine(STUDENT, "history")} == {
                past["public_id"],
                cancelled["public_id"],
            }
            assert await service.mine(OTHER) == [] and await service.mine(OTHER, "history") == []
            assert await service.export(STUDENT, past["public_id"])
            await service.cancel_registration(STUDENT, future["public_id"])
            with pytest.raises(EventNotFound):
                await service.export(STUDENT, future["public_id"])
            assert await service.export(STUDENT, future["public_id"], via_link=True)

    asyncio.run(scenario())


def test_public_contact_roundtrip_template_snapshot_and_card_escape(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions, public_contact="  @leader <public>  ")
        async with sessions.begin() as session:
            service = EventService(session)
            card = await service.get_event(event["public_id"], OTHER)
            assert card["public_contact"] == "@leader <public>"
            assert "@leader &lt;public&gt;" in event_text(card)
            assert event_dto(card)["public_contact"] == "@leader <public>"
            assert "last_editor_telegram_id" not in event_dto(card)
            template = await service.save_template(LEADER, event["public_id"], "С контактом")
            assert template["fields"]["public_contact"] == "@leader <public>"
            assert "@leader &lt;public&gt;" in template_text(template)
            copied = await service.from_template(LEADER, template["public_id"])
            assert copied["public_contact"] == "@leader <public>"
            changed = await service.update_event(
                LEADER, event["public_id"], {"public_contact": ""}, event["version"]
            )
            assert changed["public_contact"] == "" and "Ответственный:" not in event_text(changed)
            assert EventFields(public_contact="@api").model_dump(exclude_unset=True) == {
                "public_contact": "@api"
            }

    asyncio.run(scenario())


def test_calendar_navigation_remembers_filters_history_and_export_back(sessions):
    async def scenario():
        await seed(sessions)
        start, _ = week_bounds(1)
        event = await published(
            sessions, starts_at=start + timedelta(hours=15), ends_at=start + timedelta(hours=16)
        )
        screen = await step(sessions, "eventsweek:1")
        assert screen.banner_role == "events" and screen.text.startswith(legacy.text("events_list"))
        assert any(button.text == "По клубам" for button in buttons(screen))
        filters = await step(sessions, "uc:clubs:1:all:0")
        assert {button.callback_data for button in buttons(filters)} >= {
            "uc:week:1:all:0",
            "uc:week:1:cinema:0",
        }
        filtered = await step(sessions, "uc:week:1:cinema:0")
        assert "Клуб:" in filtered.text
        assert any(button.callback_data == "uc:week:2:cinema:0" for button in buttons(filtered))
        card = await step(sessions, f"event:{event['public_id']}")
        assert any(button.callback_data == "uc:week:1:cinema:0" for button in buttons(card))
        export = await step(sessions, f"evics:{event['public_id']}")
        assert export.document.filename.endswith(".ics")
        assert Calendar.from_ical(export.document.data).walk("VEVENT")
        assert buttons(export)[0].callback_data == f"event:{event['public_id']}"
        empty = await step(sessions, "my_events")
        assert "Предстоящие" in empty.text and "пока нет" in empty.text
        history = await step(sessions, "uc:mine:history:0")
        assert "История" in history.text and "пуста" in history.text
        for malformed in (
            "uc:week:no:all:0",
            "uc:week:261:all:0",
            "uc:mine:history:300",
            "uc:bad",
            "evics:x:bad",
        ):
            with pytest.raises(ValidationError):
                await step(sessions, malformed)

    asyncio.run(scenario())


def test_actual_dispatch_filter_own_history_and_paginated_card_return(sessions):
    async def scenario():
        await seed(sessions)
        start, _ = week_bounds(1)
        events = []
        for index in range(9):
            events.append(
                await published(
                    sessions,
                    title=f"Встреча номер {index}",
                    starts_at=start + timedelta(hours=12, minutes=index),
                    ends_at=start + timedelta(hours=13, minutes=index),
                )
            )
        async with sessions.begin() as session:
            service = EventService(session)
            await service.register(STUDENT, events[-1]["public_id"])
        bot, feed, _ = transport(sessions, settings())
        try:
            await feed(STUDENT, data="uc:week:1:cinema:1")
            assert "Встреча номер 8" in str(bot.sent[-1].reply_markup)
            selected = next(
                button.callback_data
                for row in bot.sent[-1].reply_markup.inline_keyboard
                for button in row
                if button.text.endswith("Встреча номер 8")
            )
            await feed(STUDENT, data=selected)
            assert any(
                button.callback_data == "uc:week:1:cinema:1"
                for row in bot.sent[-1].reply_markup.inline_keyboard
                for button in row
            )
            await feed(STUDENT, data="my_events")
            assert "Предстоящие" in shown(bot.sent[-1])
            await feed(STUDENT, data=f"event:{events[-1]['public_id']}")
            assert any(
                button.callback_data == "uc:mine:upcoming:0"
                for row in bot.sent[-1].reply_markup.inline_keyboard
                for button in row
            )
            await feed(STUDENT, data="uc:mine:history:0")
            assert "История" in shown(bot.sent[-1])
        finally:
            await bot.session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "origin", ["uc:week:1:cinema:0", "uc:mine:upcoming:0", "uc:mine:history:0"]
)
def test_full_ics_card_back_loop_retains_original_calendar_screen(sessions, origin):
    async def scenario():
        await seed(sessions)
        start, _ = week_bounds(1)
        event = await published(
            sessions,
            starts_at=start + timedelta(hours=12),
            ends_at=start + timedelta(hours=13),
            visibility="open" if ":week:" in origin else "closed",
        )
        async with sessions.begin() as session:
            service = EventService(session)
            await service.register(STUDENT, event["public_id"], via_link=True)
            if ":history:" in origin:
                row = await session.get(Event, UUID(event["id"]))
                row.starts_at = datetime.now(UTC) - timedelta(hours=2)
                row.ends_at = datetime.now(UTC) - timedelta(hours=1)
                row.registration_deadline = row.starts_at
        bot, feed, _ = transport(sessions, settings())

        def action(label):
            screen = next(
                item for item in reversed(bot.sent) if getattr(item, "reply_markup", None)
            )
            return next(
                button.callback_data
                for row in screen.reply_markup.inline_keyboard
                for button in row
                if button.text == label
            )

        try:
            await feed(STUDENT, data=origin)
            await feed(STUDENT, data=f"event:{event['public_id']}")
            await feed(STUDENT, data=action(legacy.text("event_export")))
            assert isinstance(bot.sent[-1], SendDocument)
            async with sessions() as session:
                assert (await session.get(Conversation, STUDENT)).payload[
                    "calendar_origin"
                ] == origin
            assert action("Назад") == f"event:{event['public_id']}"
            await feed(STUDENT, data=action("Назад"))
            assert action("Назад") == origin
            await feed(STUDENT, data=action("Назад"))
            assert sum(isinstance(item, SendDocument) for item in bot.sent) == 1
            async with sessions() as session:
                assert (await session.get(Conversation, STUDENT)).payload[
                    "calendar_origin"
                ] == origin
        finally:
            await bot.session.close()

    asyncio.run(scenario())
