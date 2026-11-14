"""Cross-module UI checks for actual cards and confirmed mailings."""

import asyncio

from sqlalchemy import func, select
from test_events import LEADER, STUDENT, published, seed
from test_events import sessions as sessions
from test_expanded_dispatch import settings, shown, transport

from cu_clubs import legacy
from cu_clubs.event_models import Registration
from cu_clubs.events import EventService
from cu_clubs.models import Conversation, Outbox
from cu_clubs.services import ClubService


def button(bot, label):
    return next(
        item
        for row in bot.sent[-1].reply_markup.inline_keyboard
        for item in row
        if item.text == label
    )


def test_direction_preview_upcoming_card_and_registration_return_to_club(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions)
        bot, feed, action = transport(sessions, settings())
        try:
            await feed(LEADER, data="edit:cinema:direction")
            await feed(LEADER, data=action("Hard Skill Hub"))
            assert "Предпросмотр" in shown(bot.sent[-1])
            async with sessions() as session:
                assert (await ClubService(session).get_club("cinema"))["direction"] is None
            await feed(LEADER, data=action(legacy.text("confirm")))
            async with sessions() as session:
                assert (await ClubService(session).get_club("cinema"))[
                    "direction"
                ] == "Hard Skill Hub"
            await feed(STUDENT, data="club:cinema")
            assert "Hard Skill Hub" in shown(bot.sent[-1])
            assert event["title"] in shown(bot.sent[-1])
            await feed(STUDENT, data=action(event["title"]))
            assert button(bot, "Назад").callback_data == "club:cinema"
            await feed(STUDENT, data=f"evreg:{event['public_id']}")
            assert button(bot, "Назад").callback_data == "club:cinema"
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(Registration)) == 1
            await feed(STUDENT, data=action("Назад"))
            assert event["title"] in shown(bot.sent[-1])
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_mailing_dispatch_requires_preview_and_retains_invalid_input(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions)
        async with sessions.begin() as session:
            await EventService(session).register(STUDENT, event["public_id"])
        bot, feed, action = transport(sessions, settings())
        try:
            await feed(LEADER, data=f"event:{event['public_id']}")
            await feed(LEADER, data=action("Уведомления"))
            await feed(LEADER, data=action("Написать записавшимся"))
            await feed(LEADER, text="x" * 501)
            async with sessions() as session:
                assert (await session.get(Conversation, LEADER)).state == "notification_text"
            await feed(LEADER, text="Встречаемся у входа", restart=True)
            assert "Предпросмотр" in shown(bot.sent[-1])
            assert "получателей сейчас: 1" in shown(bot.sent[-1])
            async with sessions() as session:
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(Outbox)
                        .where(Outbox.kind == "notification.broadcast")
                    )
                    == 0
                )
            confirmation = action("Отправить")
            await feed(LEADER, text="Случайный текст")
            assert action("Отправить") == confirmation
            await feed(LEADER, data=confirmation)
            assert "Получателей: 1" in shown(bot.sent[-1])
            await feed(LEADER, data=confirmation)
            async with sessions() as session:
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(Outbox)
                        .where(Outbox.kind == "notification.broadcast")
                    )
                    == 1
                )
        finally:
            await bot.session.close()

    asyncio.run(scenario())
