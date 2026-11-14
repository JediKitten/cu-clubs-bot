"""Template navigation runs through the real dispatcher and durable single screen."""

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from aiogram.methods import EditMessageMedia, SendPhoto
from aiogram.types import Update
from sqlalchemy import func, select
from test_events import LEADER, published, seed
from test_events import sessions as sessions
from test_runtime import FakeBot

from cu_clubs.config import Settings
from cu_clubs.event_models import Event
from cu_clubs.event_ui import _parse, _prompt
from cu_clubs.events import EventService
from cu_clubs.screens import UIScreen
from cu_clubs.services import ValidationError
from cu_clubs.telegram import build_dispatcher

MOSCOW = ZoneInfo("Europe/Moscow")


def text_of(method):
    if isinstance(method, EditMessageMedia):
        return method.media.caption
    return method.caption if isinstance(method, SendPhoto) else method.text


def test_club_template_to_date_only_preserves_clock_and_manual_duration(sessions):
    async def scenario():
        await seed(sessions)
        year = datetime.now(MOSCOW).year + 3
        start = datetime(year, 12, 30, 23, 30, tzinfo=MOSCOW)
        source = await published(
            sessions, starts_at=start, ends_at=start + timedelta(hours=2), room="305"
        )
        async with sessions.begin() as session:
            template = await EventService(session).save_template(
                LEADER, source["public_id"], "Ночной показ"
            )
        bot = FakeBot()
        settings = Settings(_env_file=None, environment="test")
        dispatcher = build_dispatcher(sessions, settings)
        sequence = 0

        async def step(*, data=None, text=None):
            nonlocal sequence
            sequence += 1
            sender = {"id": LEADER, "is_bot": False, "first_name": "Offline leader"}
            message = {
                "message_id": 5000 + sequence,
                "date": 1,
                "chat": {"id": LEADER, "type": "private"},
                "from": sender,
                "text": text or "screen",
            }
            payload = {"update_id": 23000 + sequence}
            if data:
                message["from"] = {"id": bot.id, "is_bot": True, "first_name": "Offline bot"}
                payload["callback_query"] = {
                    "id": str(sequence),
                    "from": sender,
                    "chat_instance": "offline",
                    "message": message,
                    "data": data,
                }
            else:
                payload["message"] = message
            await dispatcher.feed_update(bot, Update.model_validate(payload))
            return bot.sent[-1]

        def action(label):
            return next(
                button.callback_data
                for row in bot.sent[-1].reply_markup.inline_keyboard
                for button in row
                if button.text == label
            )

        async def finish_schedule():
            for _ in range(7):
                buttons = [
                    b.callback_data
                    for row in bot.sent[-1].reply_markup.inline_keyboard
                    for b in row
                    if b.callback_data
                ]
                choice = next(
                    (
                        b
                        for b in buttons
                        if b.startswith("evtime:") and b.endswith((":save", ":start"))
                    ),
                    None,
                )
                choice = choice or next(
                    b for b in buttons if b.startswith("evtime:") and b.endswith(":keep")
                )
                await step(data=choice)
                if choice.endswith(":save"):
                    return
            raise AssertionError("Schedule did not reach confirmation")

        try:
            await step(data="manageclub:cinema")
            await step(data=action("Шаблоны"))
            await step(data=f"tmpl:{template['public_id']}")
            use = action("Создать встречу")
            await step(data=use)
            date_action = action("Выбрать дату")
            event_id = date_action.split(":")[1]
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(Event)) == 2
            # The same one-use button cannot create a second draft.
            await step(data=use)
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(Event)) == 2
            await step(data=date_action)
            assert "Начало · дата" in text_of(bot.sent[-1])
            async with sessions() as session:
                first_pointer = (await session.get(UIScreen, (bot.id, LEADER))).message_id
            # A process restart does not lose the selected draft or input field.
            dispatcher = build_dispatcher(sessions, settings)
            await step(text=f"31.12.{year}")
            assert "23:30" in text_of(bot.sent[-1])
            await finish_schedule()
            async with sessions() as session:
                event = await EventService(session).get_event(event_id, LEADER)
                assert datetime.fromisoformat(event["starts_at"]).astimezone(MOSCOW) == datetime(
                    year, 12, 31, 23, 30, tzinfo=MOSCOW
                )
                assert datetime.fromisoformat(event["ends_at"]).astimezone(MOSCOW) == datetime(
                    year + 1, 1, 1, 1, 30, tzinfo=MOSCOW
                )
                assert event["room"] is None and event["registered_count"] == 0
                assert (await session.get(UIScreen, (bot.id, LEADER))).message_id == first_pointer
            await step(data=f"evf:{event_id}:end")
            await step(text=f"01.01.{year + 1} 03:00")
            await finish_schedule()
            await step(data=f"evf:{event_id}:start")
            await step(text=f"02.01.{year + 1}")
            await finish_schedule()
            async with sessions() as session:
                event = await EventService(session).get_event(event_id, LEADER)
                assert datetime.fromisoformat(event["ends_at"]).astimezone(MOSCOW) == datetime(
                    year + 1, 1, 3, 3, 0, tzinfo=MOSCOW
                )
                original = await EventService(session).get_event(source["public_id"], LEADER)
                assert datetime.fromisoformat(original["starts_at"]).astimezone(MOSCOW) == start
                assert original["room"] == "305"
            await step(data=f"evf:{event_id}:start")
            await step(text="31.12.9999")
            await step(data=action("Оставить 23:30"))
            assert "допустимый диапазон" in text_of(bot.sent[-1])
            async with sessions() as session:
                assert (await EventService(session).get_event(event_id, LEADER))[
                    "starts_at"
                ] == event["starts_at"]
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_template_date_only_hint_does_not_change_published_reschedule_contract():
    from types import SimpleNamespace

    event = {
        "status": "published",
        "starts_at": "2030-01-01T20:30:00+00:00",
        "public_id": "a" * 32,
        "template_start_time": "23:30",
        "template_duration_minutes": 120,
    }
    state = SimpleNamespace(payload={"field": "starts_at", "token": "offline"})
    assert "Пришли только дату" not in _prompt(state, event).text
    with pytest.raises(ValidationError):
        _parse("starts_at", "02.01.2030", event)
