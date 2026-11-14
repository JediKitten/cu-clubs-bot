"""Actual Telegram transport boundary for surveys, analytics and API consent."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

from aiogram.methods import EditMessageMedia, SendPhoto
from aiogram.types import Update
from pydantic import SecretStr
from sqlalchemy import func, select
from test_events import LEADER, STUDENT, published, seed
from test_events import sessions as sessions
from test_runtime import FakeBot

from cu_clubs import worker
from cu_clubs.api_models import ApiLink
from cu_clubs.api_service import SCOPES, ApiService
from cu_clubs.attendance import AttendanceService
from cu_clubs.attendance_models import Attendance
from cu_clubs.config import Settings
from cu_clubs.event_models import Registration
from cu_clubs.models import Conversation, Outbox
from cu_clubs.survey_models import SurveyResponse
from cu_clubs.surveys import SurveysService
from cu_clubs.telegram import build_dispatcher


def shown(method):
    if isinstance(method, EditMessageMedia):
        return method.media.caption
    return method.caption if isinstance(method, SendPhoto) else method.text


def transport(sessions, settings):
    bot = FakeBot()
    dispatcher = build_dispatcher(sessions, settings)
    sequence = 0

    async def feed(actor, *, data=None, text=None, restart=False):
        nonlocal sequence, dispatcher
        if restart:
            dispatcher = build_dispatcher(sessions, settings)
        sequence += 1
        user = {"id": actor, "is_bot": False, "first_name": "Offline tester"}
        message = {
            "message_id": sequence,
            "date": 1700000000,
            "chat": {"id": actor, "type": "private"},
            "from": user,
        }
        if data:
            message["from"] = {"id": bot.id, "is_bot": True, "first_name": "Bot"}
            payload = {
                "callback_query": {
                    "id": str(sequence),
                    "from": user,
                    "message": message,
                    "chat_instance": "offline",
                    "data": data,
                }
            }
        else:
            message["text"] = text
            payload = {"message": message}
        await dispatcher.feed_update(
            bot, Update.model_validate({"update_id": 80000 + sequence, **payload})
        )
        return bot.sent[-1]

    def action(label):
        return next(
            b.callback_data
            for row in bot.sent[-1].reply_markup.inline_keyboard
            for b in row
            if b.text == label
        )

    return bot, feed, action


def settings():
    return Settings(
        _env_file=None,
        environment="test",
        telegram_bot_username="offline_test_bot",
        verification_secret=SecretStr("OFFLINE_EXPANDED_DISPATCH_SECRET_32"),
    )


def test_survey_text_and_private_report_use_actual_dispatcher(sessions):
    async def scenario():
        await seed(sessions)
        config = settings()
        async with sessions.begin() as session:
            await SurveysService(session).save_club(
                LEADER, "cinema", [{"kind": "text", "prompt": "Что улучшить?", "required": True}]
            )
        start = datetime.now(UTC) - timedelta(hours=3)
        event = await published(sessions, starts_at=start, ends_at=start + timedelta(hours=1))
        async with sessions.begin() as session:
            await AttendanceService(session, config.verification_secret).mark_student(
                LEADER, event["public_id"], f"Student {STUDENT}", f"u{STUDENT}"
            )
        bot, feed, action = transport(sessions, config)
        try:
            await feed(STUDENT, data=f"su:{event['public_id']}")
            await feed(STUDENT, data=action("5"))
            async with sessions() as session:
                response = await session.scalar(select(SurveyResponse))
                assert response.rating == 5 and response.completed_at is None
            await feed(STUDENT, text="Больше обсуждений", restart=True)
            assert "Ответы сохранены" in shown(bot.sent[-1])
            await feed(LEADER, data=f"anevent:{event['public_id']}")
            assert "5,0 из 5" in shown(bot.sent[-1])
            await feed(LEADER, data=action("Обратная связь"))
            assert "Больше обсуждений" in shown(bot.sent[-1])
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_api_deep_link_requires_personal_confirmation_without_auto_registration(sessions):
    async def scenario():
        await seed(sessions)
        config = settings()
        async with sessions.begin() as session:
            api = ApiService(session, config)
            issued = await api.issue_client(LEADER, "cinema", "Offline client", SCOPES)
            client = await api.authenticate(issued["api_key"])
            link = await api.create_link(client, STUDENT)
        bot, feed, action = transport(sessions, config)
        try:
            raw = link["verification_uri"].split("start=l_", 1)[1]
            await feed(STUDENT, text=f"/start l_{raw}")
            async with sessions() as session:
                assert (await session.get(ApiLink, UUID(link["id"]))).approved_at is None
            await feed(STUDENT, data=action("Разрешить подключение"))
            async with sessions() as session:
                assert (await session.get(ApiLink, UUID(link["id"]))).approved_at is not None
                assert await session.scalar(select(func.count()).select_from(Registration)) == 0
                assert await session.scalar(select(func.count()).select_from(Attendance)) == 0
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_survey_invitation_does_not_replace_an_active_input_screen(sessions):
    async def scenario():
        async with sessions.begin() as session:
            session.add(Conversation(telegram_id=STUDENT, state="event_edit", payload={}))
            session.add(
                Outbox(
                    kind="survey.invitation",
                    recipient_telegram_id=STUDENT,
                    payload={"event": "offline"},
                    dedupe_key="offline-survey-defer",
                )
            )
        assert await worker.claim_outbox(sessions) is None
        async with sessions.begin() as session:
            (await session.get(Conversation, STUDENT)).state = "idle"
        item = await worker.claim_outbox(sessions)
        assert item["kind"] == "survey.invitation" and item["attempts"] == 1

    asyncio.run(scenario())
