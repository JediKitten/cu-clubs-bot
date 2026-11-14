"""Fake-clock notifications acceptance using real PostgreSQL and no external sends."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select
from test_events import EDITOR, LEADER, OTHER, PARTNER, STUDENT, published, seed
from test_events import sessions as sessions

from cu_clubs.attendance import AttendanceService
from cu_clubs.event_models import Event, Registration
from cu_clubs.events import EventService
from cu_clubs.models import Membership, Outbox
from cu_clubs.notification_models import Broadcast
from cu_clubs.notifications import NotificationsService
from cu_clubs.notifications_ui import handle_notifications
from cu_clubs.services import ClubService, PermissionDenied, ValidationError

NOW = datetime(2030, 5, 10, 13, tzinfo=UTC)  # Friday 16:00 Moscow.
SECRET = "notifications-synthetic-test-secret-32"


def item(row):
    return {
        "id": row.id,
        "kind": row.kind,
        "recipient": row.recipient_telegram_id,
        "payload": row.payload,
        "attempts": row.attempts,
    }


async def meeting(factory, *, hours=24):
    await seed(factory)
    event = await published(
        factory,
        starts_at=NOW + timedelta(hours=hours),
        ends_at=NOW + timedelta(hours=hours + 2),
        room="B310",
    )
    async with factory.begin() as session:
        await EventService(session).register(STUDENT, event["public_id"])
    return event


def button(screen, text):
    return next(
        button.callback_data
        for row in screen.markup.inline_keyboard
        for button in row
        if button.text == text
    )


def test_reminder_dedupe_fresh_location_and_reschedule_cancel(sessions):
    async def scenario():
        event = await meeting(sessions)
        async with sessions.begin() as session:
            notices = NotificationsService(session, now=NOW)
            await notices.schedule()
            assert await notices.schedule() == 0
            row = await session.scalar(select(Outbox).where(Outbox.kind == "notification.reminder"))
            assert row.payload["hours"] == 24
            model = await session.scalar(select(Event).where(Event.public_id == event["public_id"]))
            model.room = "B999 <updated>"
            await session.flush()
            delivery = await notices.delivery(item(row))
            assert (
                "B999 &lt;updated&gt;" in delivery["text"]
                and "11.05.2030 16:00" in delivery["text"]
            )
            model.starts_at += timedelta(days=1)
            model.ends_at += timedelta(days=1)
            await session.flush()
            assert await notices.delivery(item(row)) is None
            rescheduled = NotificationsService(session, now=NOW + timedelta(days=1))
            await rescheduled.schedule()
            rows = list(
                await session.scalars(select(Outbox).where(Outbox.kind == "notification.reminder"))
            )
            assert len(rows) == 2
            current = next(
                row for row in rows if row.payload["schedule"] == model.starts_at.isoformat()
            )
            registration = await session.get(
                Registration, (model.id, (await ClubService(session)._account(STUDENT)).person_id)
            )
            registration.status = "cancelled"
            await session.flush()
            assert await rescheduled.delivery(item(current)) is None
            registration.status = "active"
            model.status = "cancelled"
            await session.flush()
            assert await rescheduled.delivery(item(current)) is None

    asyncio.run(scenario())


def test_hour_reminder_window_and_friday_zero_meeting_leaders(sessions):
    async def scenario():
        await meeting(sessions, hours=1)
        async with sessions.begin() as session:
            before = NotificationsService(session, now=NOW - timedelta(minutes=1))
            assert await before.schedule() == 0
            current = NotificationsService(session, now=NOW)
            await current.schedule()
            weekly = list(
                await session.scalars(select(Outbox).where(Outbox.kind == "notification.weekly"))
            )
            assert {row.recipient_telegram_id for row in weekly} == {LEADER, PARTNER}
            for row in weekly:
                view = await current.delivery(item(row))
                assert "Пока нет встреч" in view["text"]
                assert "docs.google.com" not in view["text"]
                assert view["callback"] == "manage"
            reminder = await session.scalar(
                select(Outbox).where(Outbox.kind == "notification.reminder")
            )
            assert reminder.payload["hours"] == 1
            assert "Через час" in (await current.delivery(item(reminder)))["text"]
            assert (
                await NotificationsService(session, now=NOW + timedelta(minutes=5)).delivery(
                    item(reminder)
                )
                is not None
            )
            assert (
                await NotificationsService(
                    session, now=NOW + timedelta(minutes=5, seconds=1)
                ).delivery(item(reminder))
                is None
            )
            assert (
                await NotificationsService(session, now=NOW + timedelta(minutes=11)).delivery(
                    item(reminder)
                )
                is None
            )
            assert await current.schedule() == 0
            account = await ClubService(session)._account(PARTNER)
            await session.execute(
                delete(Membership).where(Membership.person_id == account.person_id)
            )
            removed = next(row for row in weekly if row.recipient_telegram_id == PARTNER)
            assert await current.delivery(item(removed)) is None

    asyncio.run(scenario())


def test_broadcast_preview_recounts_confirm_once_and_rechecks_acl(sessions):
    async def scenario():
        event = await meeting(sessions)
        async with sessions.begin() as session:
            service = NotificationsService(session, now=NOW)
            preview = await service.prepare(
                EDITOR, event["public_id"], "registered", "<Private update>"
            )
            assert preview["count"] == 1
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Outbox)
                    .where(Outbox.kind == "notification.broadcast")
                )
                == 0
            )
            await EventService(session).register(OTHER, event["public_id"])
            result = await service.confirm(EDITOR, preview["id"])
            assert result["queued"] == 2
            assert (await service.confirm(EDITOR, preview["id"]))["already_confirmed"]
            rows = list(
                await session.scalars(select(Outbox).where(Outbox.kind == "notification.broadcast"))
            )
            assert len(rows) == 2 and all("Private" not in str(row.payload) for row in rows)
            view = await service.delivery(item(rows[0]))
            assert "&lt;Private update&gt;" in view["text"]
            account = await ClubService(session)._account(EDITOR)
            member = await session.scalar(
                select(Membership).where(Membership.person_id == account.person_id)
            )
            member.manage_events = False
            await session.flush()
            assert await service.delivery(item(rows[0])) is None
            with pytest.raises(PermissionDenied):
                await service.confirm(EDITOR, preview["id"])
            with pytest.raises(PermissionDenied):
                await service.prepare(STUDENT, event["public_id"], "registered", "No access")

    asyncio.run(scenario())


def test_attended_audience_and_queue_retry_privacy(sessions):
    async def scenario():
        event = await meeting(sessions)
        async with sessions.begin() as session:
            attendance = AttendanceService(session, SECRET)
            await attendance.mark_manual(
                LEADER, event["public_id"], (await ClubService(session)._account(OTHER)).person_id
            )
            notices = NotificationsService(session, now=NOW)
            draft = await notices.prepare(
                LEADER, event["public_id"], "attended", "Thanks for attending"
            )
            assert draft["count"] == 1
            await notices.confirm(LEADER, draft["id"])
            row = await session.scalar(
                select(Outbox).where(Outbox.kind == "notification.broadcast")
            )
            assert row.recipient_telegram_id == OTHER
            row.status, row.last_error = "failed", "TelegramForbiddenError"
            await session.flush()
            with pytest.raises(PermissionDenied):
                await notices.queue(STUDENT, event["public_id"])
            assert (await notices.queue(LEADER, event["public_id"]))["counts"]["failed"] == 1
            assert await notices.retry(LEADER, event["public_id"]) == 1
            row.status = "uncertain"
            await session.flush()
            assert await notices.retry(LEADER, event["public_id"]) == 0

    asyncio.run(scenario())


def test_ui_preview_cancel_confirm_and_stale_callback(sessions):
    async def scenario():
        event = await meeting(sessions)
        state = SimpleNamespace(state="idle", payload={})
        async with sessions.begin() as session:

            async def click(data="", message=None):
                return await handle_notifications(session, LEADER, state, message, data, None)

            menu = await click(f"nt:{event['public_id']}")
            prompt = await click(button(menu, "Написать записавшимся"))
            assert "зарегистрировались" in prompt.text
            invalid = await click(message="X" * 501)
            assert "не длиннее 500" in invalid.text
            assert state.state == "notification_text"
            preview = await click(message="<Example message>")
            assert "&lt;Example message&gt;" in preview.text and "сейчас: 1" in preview.text
            callback = button(preview, "Отправить")
            retained = await click(message=SimpleNamespace(text="Unconfirmed extra input"))
            assert "&lt;Example message&gt;" in retained.text
            assert button(retained, "Отправить") == callback
            assert state.state == "notification_preview"
            await click(button(preview, "Отмена"))
            with pytest.raises(ValidationError):
                await click(callback)
            await click(button(menu, "Написать записавшимся"))
            preview = await click(message="Confirmed message")
            await click(button(preview, "Отправить"))
            assert state.state == "idle"
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Broadcast)
                    .where(Broadcast.confirmed_at.is_not(None))
                )
                == 1
            )

    asyncio.run(scenario())


def test_worker_delivery_retry_and_stale_suppression_without_network(sessions, monkeypatch):
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendMessage

    from cu_clubs import notifications, worker

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    class Bot:
        def __init__(self):
            self.calls = []
            self.blocked = False
            self.id = 8001
            self.deleted = []
            self.next_id = 122

        async def delete_message(self, chat_id, message_id):
            self.deleted.append((chat_id, message_id))
            return True

        async def send_message(self, chat_id, text, **kwargs):
            recipient = chat_id
            if self.blocked:
                raise TelegramForbiddenError(
                    method=SendMessage(chat_id=recipient, text=text), message="Synthetic forbidden"
                )
            self.calls.append((recipient, text))
            self.next_id += 1
            return SimpleNamespace(message_id=self.next_id)

    monkeypatch.setattr(worker, "utcnow", lambda: NOW)
    monkeypatch.setattr(notifications, "datetime", Clock)

    async def scenario():
        event = await meeting(sessions)
        async with sessions.begin() as session:
            await session.execute(delete(Outbox))
            await NotificationsService(session, now=NOW).schedule()
            # Keep this test focused on event reminders, not Friday leader notices.
            await session.execute(delete(Outbox).where(Outbox.kind != "notification.reminder"))
        bot = Bot()
        outgoing = await worker.claim_outbox(sessions)
        assert outgoing is not None
        bot.blocked = True
        await worker.process_outbox(sessions, bot, outgoing, None)
        async with sessions.begin() as session:
            row = await session.get(Outbox, outgoing["id"])
            assert row.status == "failed" and row.last_error == "TelegramForbiddenError"
            await NotificationsService(session, now=NOW).retry(LEADER, event["public_id"])
        bot.blocked = False
        outgoing = await worker.claim_outbox(sessions)
        await worker.process_outbox(sessions, bot, outgoing, None)
        assert len(bot.calls) == 1 and bot.calls[0][0] == STUDENT
        from cu_clubs.screens import ScreenManager, UIScreen

        async with sessions.begin() as session:
            current = await session.get(UIScreen, (bot.id, STUDENT))
            assert current.message_id == 123
        await ScreenManager(sessions, bot).show(STUDENT, "Event navigation", force_new=True)
        assert (STUDENT, 123) in bot.deleted
        async with sessions.begin() as session:
            row = await session.get(Outbox, outgoing["id"])
            assert row.status == "sent" and row.telegram_message_id == 123
            row.status, row.available_at = "pending", NOW
            model = await session.scalar(select(Event).where(Event.public_id == event["public_id"]))
            model.status = "cancelled"
        outgoing = await worker.claim_outbox(sessions)
        await worker.process_outbox(sessions, bot, outgoing, None)
        assert len(bot.calls) == 2

    asyncio.run(scenario())


def test_worker_defers_active_input_at_claim_and_delivery(sessions, monkeypatch):
    from cu_clubs import worker
    from cu_clubs.models import Conversation
    from cu_clubs.screens import UIScreen

    class Bot:
        id = 8001

        async def send_message(self, **kwargs):
            pytest.fail("A notification must not overwrite active input")

    monkeypatch.setattr(worker, "utcnow", lambda: NOW)

    async def scenario():
        await meeting(sessions)
        async with sessions.begin() as session:
            await session.execute(delete(Outbox))
            await NotificationsService(session, now=NOW).schedule()
            await session.execute(delete(Outbox).where(Outbox.kind != "notification.reminder"))
            session.add(
                Conversation(
                    telegram_id=STUDENT, state="notification_text", payload={"retained": "input"}
                )
            )
        assert await worker.claim_outbox(sessions) is None
        async with sessions.begin() as session:
            conversation = await session.get(Conversation, STUDENT)
            conversation.state = "idle"
        outgoing = await worker.claim_outbox(sessions)
        assert outgoing is not None
        # A dialogue can start after the first worker eligibility check.
        async with sessions.begin() as session:
            conversation = await session.get(Conversation, STUDENT)
            conversation.state = "event_edit"
        await worker.process_outbox(sessions, Bot(), outgoing, None)
        async with sessions.begin() as session:
            conversation = await session.get(Conversation, STUDENT)
            row = await session.get(Outbox, outgoing["id"])
            assert row.status == "pending" and row.lease_until is None
            assert conversation.state == "event_edit" and conversation.payload == {
                "retained": "input"
            }
            assert await session.get(UIScreen, (Bot.id, STUDENT)) is None

    asyncio.run(scenario())
