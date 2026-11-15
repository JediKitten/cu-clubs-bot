"""Category onboarding at the actual dispatcher boundary, using synthetic profiles."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select
from test_events import LEADER, published, seed
from test_events import sessions as sessions
from test_expanded_dispatch import settings, shown, transport

from cu_clubs import legacy
from cu_clubs.api_models import ApiLink
from cu_clubs.api_service import SCOPES, ApiService
from cu_clubs.attendance import AttendanceService
from cu_clubs.attendance_models import Attendance
from cu_clubs.event_models import Registration
from cu_clubs.identity_models import EmailChallenge
from cu_clubs.models import Conversation
from cu_clubs.services import ClubService
from cu_clubs.surveys import SurveysService

NEW = 992001


def enable(factory):
    factory.configure(info={"test_self_registration_enabled": True})
    config = settings()
    config.test_self_registration_enabled = True
    return config


def labels(bot):
    return [b.text for row in bot.sent[-1].reply_markup.inline_keyboard for b in row]


async def complete_university(feed, action, *, name="Иванов Иван Иванович", login="i.ivanov"):
    await feed(NEW, text=name)
    await feed(NEW, text=login, restart=True)
    await feed(NEW, data=action("Подтвердить"))


@pytest.mark.parametrize("category", ["Студент", "Сотрудник"])
def test_category_once_profile_change_cancel_and_no_email(sessions, category):
    async def scenario():
        config = enable(sessions)
        bot, feed, action = transport(sessions, config)
        try:
            await feed(NEW, text="/start")
            assert labels(bot) == ["Студент", "Сотрудник", "Абитуриент", "Внешний"]
            old_pick = action(category)
            await feed(NEW, data=old_pick, restart=True)
            assert "Укажи ФИО" in shown(bot.sent[-1])
            await complete_university(feed, action)
            assert "Кто ты?" not in shown(bot.sent[-1])
            await feed(NEW, text="/start")
            assert "Кто ты?" not in shown(bot.sent[-1])
            await feed(NEW, data="profile")
            assert category in shown(bot.sent[-1])
            assert "i.ivanov" in shown(bot.sent[-1])
            assert "Иванов Иван Иванович" in shown(bot.sent[-1])
            assert "Подтвердить почту" not in labels(bot)
            await feed(NEW, data=action("Изменить категорию"))
            await feed(NEW, data=action("Назад"))
            assert category in shown(bot.sent[-1])
            await feed(NEW, data=old_pick)
            assert "устарела" in shown(bot.sent[-1])
            async with sessions() as session:
                person = (await ClubService(session)._account(NEW)).person
                assert person.declared_category == ("student" if category == "Студент" else "staff")
                assert person.verified_at is None and person.time_login == "i.ivanov"
                assert person.full_name == "Иванов Иван Иванович"
                assert await session.scalar(select(func.count()).select_from(EmailChallenge)) == 0
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_closed_event_origin_is_preserved_without_auto_registration(sessions):
    async def scenario():
        await seed(sessions)
        config = enable(sessions)
        event = await published(sessions, visibility="closed")
        bot, feed, action = transport(sessions, config)
        try:
            await feed(NEW, text=f"/start e_{event['public_id']}")
            await feed(NEW, data=action("Студент"), restart=True)
            await complete_university(feed, action)
            assert event["title"] in shown(bot.sent[-1])
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(Registration)) == 0
            await feed(NEW, data=action(legacy.text("register")))
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(Registration)) == 1
        finally:
            await bot.session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("category", ["Абитуриент", "Внешний"])
def test_guest_qr_name_back_and_confirmation_survive_restart(sessions, category):
    async def scenario():
        await seed(sessions)
        config = enable(sessions)
        event = await published(sessions)
        async with sessions.begin() as session:
            qr = await AttendanceService(session, config.verification_secret).event_qr(
                LEADER, event["public_id"]
            )
        bot, feed, action = transport(sessions, config)
        try:
            await feed(NEW, text="/start a_" + qr["token"])
            await feed(NEW, data=action(category))
            assert "Укажи ФИО" in shown(bot.sent[-1])
            await feed(NEW, text="Иванов Иван Иванович", restart=True)
            await feed(NEW, data=action("Назад"))
            assert "Иванов Иван Иванович" in shown(bot.sent[-1])
            await feed(NEW, text="Петров Пётр Петрович")
            await feed(NEW, data=action("Подтвердить"), restart=True)
            assert "Отметить посещение" in labels(bot)
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(Attendance)) == 0
                person = (await ClubService(session)._account(NEW)).person
                assert person.full_name == "Петров Пётр Петрович"
                assert person.verified_at is None
            await feed(NEW, data=action("Отметить посещение"))
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(Attendance)) == 1
                assert await session.scalar(select(func.count()).select_from(Registration)) == 0
            await feed(NEW, data=f"evreg:{event['public_id']}")
            assert "Предварительная запись доступна" in shown(bot.sent[-1])
            assert "Подтвердить почту" not in labels(bot)
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_new_start_replaces_old_pending_origin(sessions):
    async def scenario():
        await seed(sessions)
        config = enable(sessions)
        event = await published(sessions)
        bot, feed, action = transport(sessions, config)
        try:
            await feed(NEW, text=f"/start e_{event['public_id']}")
            old = action("Студент")
            await feed(NEW, text="/start c_cinema")
            assert action("Студент") != old
            await feed(NEW, data=action("Студент"))
            await complete_university(feed, action)
            assert "Киноклуб" in shown(bot.sent[-1])
            assert any(
                b.callback_data == "clubs"
                for row in bot.sent[-1].reply_markup.inline_keyboard
                for b in row
            )
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_category_api_deep_link_still_needs_separate_consent(sessions):
    async def scenario():
        await seed(sessions)
        config = enable(sessions)
        async with sessions.begin() as session:
            api = ApiService(session, config)
            issued = await api.issue_client(LEADER, "cinema", "Offline test client", SCOPES)
            client = await api.authenticate(issued["api_key"])
            link = await api.create_link(client, NEW)
        bot, feed, action = transport(sessions, config)
        try:
            raw = link["verification_uri"].split("start=l_", 1)[1]
            await feed(NEW, text="/start l_" + raw)
            await feed(NEW, data=action("Сотрудник"), restart=True)
            await complete_university(feed, action)
            assert "Разрешить подключение" in labels(bot)
            async with sessions() as session:
                assert (await session.get(ApiLink, UUID(link["id"]))).approved_at is None
            await feed(NEW, data=action("Разрешить подключение"))
            async with sessions() as session:
                assert (await session.get(ApiLink, UUID(link["id"]))).approved_at is not None
                assert await session.scalar(select(func.count()).select_from(Registration)) == 0
                assert await session.scalar(select(func.count()).select_from(Attendance)) == 0
                assert (await ClubService(session)._account(NEW)).person.verified_at is None
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_declared_attendee_receives_post_event_survey(sessions):
    async def scenario():
        await seed(sessions)
        config = enable(sessions)
        event = await published(sessions)
        bot, feed, action = transport(sessions, config)
        try:
            await feed(NEW, text="/start")
            await feed(NEW, data=action("Студент"))
            await complete_university(feed, action)
            async with sessions.begin() as session:
                service = AttendanceService(session, config.verification_secret)
                qr = await service.event_qr(LEADER, event["public_id"])
                await service.mark_event_qr(NEW, qr["token"])
                model = await service.events._event(event["public_id"], lock=True)
                model.starts_at = datetime.now(UTC) - timedelta(hours=2)
                model.ends_at = datetime.now(UTC) - timedelta(hours=1)
                model.registration_deadline = model.starts_at
                assert await SurveysService(session).enqueue_invitations() == 1
                assert (await session.get(Conversation, NEW)).state == "idle"
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_university_required_fields_back_validation_and_incomplete_existing_profile(sessions):
    async def scenario():
        config = enable(sessions)
        bot, feed, action = transport(sessions, config)
        try:
            await feed(NEW, text="/start")
            await feed(NEW, data=action("Студент"))
            await feed(NEW, text="Иван")
            assert "полностью" in shown(bot.sent[-1])
            await feed(NEW, text="Иванов Иван Иванович")
            await feed(NEW, text="bad login")
            assert "без пробелов" in shown(bot.sent[-1])
            await feed(NEW, text="i.ivanov")
            await feed(NEW, data=action("Назад"))
            assert "i.ivanov" in shown(bot.sent[-1])
            await feed(NEW, data=action("Назад"))
            assert "Иванов Иван Иванович" in shown(bot.sent[-1])
            await complete_university(feed, action)
            async with sessions.begin() as session:
                person = (await ClubService(session)._account(NEW)).person
                person.time_login = None
            await feed(NEW, data="profile", restart=True)
            assert "Укажи ФИО" in shown(bot.sent[-1])
            await complete_university(feed, action, login="restored.login")
            assert "restored.login" in shown(bot.sent[-1])
            assert "Иванов Иван Иванович" in shown(bot.sent[-1])
            async with sessions() as session:
                assert (await ClubService(session)._account(NEW)).person.verified_at is None
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_duplicate_login_keeps_api_origin_and_recovers_in_current_window(sessions):
    async def scenario():
        await seed(sessions)
        config = enable(sessions)
        async with sessions.begin() as session:
            from cu_clubs.models import Person

            session.add(Person(full_name="Historical Original", time_login="taken.login"))
            api = ApiService(session, config)
            issued = await api.issue_client(LEADER, "cinema", "Collision test client", SCOPES)
            client = await api.authenticate(issued["api_key"])
            link = await api.create_link(client, NEW)
        bot, feed, action = transport(sessions, config)
        try:
            raw = link["verification_uri"].split("start=l_", 1)[1]
            await feed(NEW, text="/start l_" + raw)
            await feed(NEW, data=action("Сотрудник"))
            await feed(NEW, text="Иванов Иван Иванович")
            await feed(NEW, text=" TAKEN.LOGIN ")
            stale_confirm = action("Подтвердить")
            await feed(NEW, data=stale_confirm, restart=True)
            assert "логин уже используется" in shown(bot.sent[-1])
            assert "Иванов Иван Иванович" in shown(bot.sent[-1])
            assert "taken.login" in shown(bot.sent[-1])
            assert labels(bot) == ["Назад"]
            async with sessions() as session:
                state = await session.get(Conversation, NEW)
                assert state.state == "category_login"
                assert state.payload["resume"] == "apilink:" + link["id"]
                person = (await ClubService(session)._account(NEW)).person
                assert person.time_login is None and person.declared_category is None
                assert (await session.get(ApiLink, UUID(link["id"]))).approved_at is None
            await feed(NEW, text="free.login", restart=True)
            assert action("Подтвердить") != stale_confirm
            await feed(NEW, data=action("Подтвердить"))
            assert "Разрешить подключение" in labels(bot)
            async with sessions() as session:
                person = (await ClubService(session)._account(NEW)).person
                assert person.time_login == "free.login" and person.verified_at is None
                assert (await session.get(ApiLink, UUID(link["id"]))).approved_at is None
        finally:
            await bot.session.close()

    asyncio.run(scenario())
