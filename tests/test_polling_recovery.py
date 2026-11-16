"""Regression for real callback messages containing partial link preview options."""

import asyncio

import pytest
from aiogram.exceptions import TelegramServerError
from aiogram.methods import GetUpdates
from aiogram.types import Update
from sqlalchemy import select
from test_runtime import LEADER, FakeBot, run
from test_runtime import runtime_database_url as runtime_database_url

from cu_clubs import legacy, worker
from cu_clubs.inbox_crypto import open_update
from cu_clubs.models import Outbox, TelegramUpdate
from cu_clubs.screens import ScreenManager
from cu_clubs.ui_common import keyboard

pytestmark = pytest.mark.integration


def test_partial_preview_callback_survives_telegram_server_error(runtime_database_url, monkeypatch):
    async def no_delay(_):
        pass

    monkeypatch.setattr(worker.asyncio, "sleep", no_delay)

    async def scenario(sessions, settings):
        update = Update.model_validate(
            {
                "update_id": 991,
                "callback_query": {
                    "id": "offline",
                    "chat_instance": "offline",
                    "data": "club:cinema",
                    "from": {"id": LEADER, "is_bot": False, "first_name": "Tester"},
                    "message": {
                        "message_id": 1,
                        "date": 1700000000,
                        "chat": {"id": LEADER, "type": "private"},
                        "link_preview_options": {"is_disabled": True},
                    },
                },
            }
        )

        class PollingBot:
            calls = 0

            async def get_updates(self, *, offset, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    assert offset is None
                    raise TelegramServerError(method=GetUpdates(), message="Bad Gateway")
                if self.calls == 2:
                    assert offset is None
                    return [update]
                assert offset == 992
                async with sessions() as session:
                    row = await session.scalar(select(TelegramUpdate))
                    payload = open_update(row.payload, settings.verification_secret)
                    preview = payload["callback_query"]["message"]["link_preview_options"]
                    assert preview == {"is_disabled": True}
                    restored = Update.model_validate(payload)
                    assert restored.callback_query.data == "club:cinema"
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await worker.poll_updates(PollingBot(), sessions, settings)

    run(runtime_database_url, scenario)


def test_legacy_notification_is_removed_on_url_navigation(runtime_database_url):
    async def scenario(sessions, settings):
        bot = FakeBot()
        try:
            manager = ScreenManager(sessions, bot)
            await manager.show(LEADER, "Current menu")
            async with sessions.begin() as session:
                session.add(
                    Outbox(
                        kind="club.created",
                        recipient_telegram_id=LEADER,
                        payload={"public_id": "cinema"},
                        dedupe_key="historical-notice",
                        status="sent",
                        telegram_message_id=555,
                    )
                )
            # An old URL button arrives as /start; it has no callback reference.
            await manager.show(LEADER, "Club card")
            assert bot.deleted.count(555) == 1
            await manager.show(LEADER, "Another page")
            assert bot.deleted.count(555) == 1
        finally:
            await bot.session.close()

    run(runtime_database_url, scenario)


def test_back_button_override_keeps_navigation_arrows():
    markup = keyboard([(legacy.text("back"), "menu"), ("←", "page:previous")])
    assert [button.text for button in markup.inline_keyboard[0]] == ["Назад", "←"]


def test_unexpected_text_keeps_the_active_screen(runtime_database_url):
    from cu_clubs.telegram import build_dispatcher

    async def scenario(sessions, settings):
        bot = FakeBot()
        try:
            await ScreenManager(sessions, bot).show(
                LEADER, "Профиль клуба", keyboard([("Редактировать", "editmenu:cinema")])
            )
            dispatcher = build_dispatcher(sessions, settings)
            await dispatcher.feed_update(
                bot,
                Update.model_validate(
                    {
                        "update_id": 777,
                        "message": {
                            "message_id": 888,
                            "date": 1700000000,
                            "chat": {"id": LEADER, "type": "private"},
                            "from": {"id": LEADER, "is_bot": False, "first_name": "Tester"},
                            "text": "Случайный текст",
                        },
                    }
                ),
            )
            # Every screen now has owner-supplied artwork; visible text is its caption.
            assert bot.sent[-1].caption == "Профиль клуба"
            assert (
                bot.sent[-1].reply_markup.inline_keyboard[0][0].callback_data == "editmenu:cinema"
            )
        finally:
            await bot.session.close()

    run(runtime_database_url, scenario)
