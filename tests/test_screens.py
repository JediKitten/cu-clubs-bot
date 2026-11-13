"""Single-screen acceptance checks use FakeBot and an isolated real PG schema."""

import asyncio
import hashlib
import os
from html import unescape
from pathlib import Path
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from cu_clubs import legacy
from cu_clubs.config import Settings
from cu_clubs.db import make_database
from cu_clubs.screens import ScreenManager, UIScreen, _pages, _units

CHAT, BOT = 7001, 8001


def message(message_id, *, sender=BOT, photo=False):
    payload = {
        "message_id": message_id,
        "date": 1,
        "chat": {"id": CHAT, "type": "private"},
        "from": {"id": sender, "is_bot": sender != CHAT, "first_name": "Test"},
    }
    if photo:
        payload["photo"] = [
            {"file_id": "photo-file-id", "file_unique_id": "unique", "width": 100, "height": 100}
        ]
    else:
        payload["text"] = "screen"
    return Message.model_validate(payload)


def callback(message_id, *, sender=BOT, data="menu"):
    return CallbackQuery.model_validate(
        {
            "id": "test-callback",
            "from": {"id": CHAT, "is_bot": False, "first_name": "User"},
            "chat_instance": "test",
            "data": data,
            "message": message(message_id, sender=sender).model_dump(mode="json", by_alias=True),
        }
    )


class FakeBot:
    def __init__(self, bot_id=BOT):
        self.id = bot_id
        self.calls = []
        self.next_id = 100
        self.failure = None

    async def _result(self, method, kwargs, *, photo=False):
        self.calls.append((method, kwargs))
        await asyncio.sleep(0.005)
        if self.failure and self.failure[0] == method:
            error, self.failure = self.failure[1], None
            raise error
        if method == "delete":
            return True
        if method.startswith("send"):
            self.next_id += 1
            message_id = self.next_id
        else:
            message_id = kwargs["message_id"]
        return message(message_id, sender=self.id, photo=photo)

    async def send_message(self, **kwargs):
        return await self._result("send_text", kwargs)

    async def send_photo(self, **kwargs):
        return await self._result("send_photo", kwargs, photo=True)

    async def edit_message_text(self, **kwargs):
        return await self._result("edit_text", kwargs)

    async def edit_message_media(self, **kwargs):
        return await self._result("edit_photo", kwargs, photo=True)

    async def delete_message(self, **kwargs):
        return await self._result("delete", kwargs)


@pytest.fixture
def database_url():
    configured = os.environ.get("TEST_DATABASE_URL")
    if not configured:
        pytest.skip("Set TEST_DATABASE_URL; screen tests never create their own cluster")
    schema = f"test_screens_{uuid4().hex}"
    engine = create_engine(configured)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    scoped = make_url(configured).update_query_dict({"options": f"-csearch_path={schema}"})
    try:
        yield scoped.render_as_string(hide_password=False)
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def run(database_url, scenario):
    async def exercise():
        settings = Settings(_env_file=None, database_url=SecretStr(database_url))
        engine, sessions = make_database(settings)
        try:
            await scenario(sessions)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


async def stored(sessions, bot_id=BOT):
    async with sessions() as session:
        return await session.get(UIScreen, (bot_id, CHAT))


def test_short_legacy_html_is_preserved_and_long_pages_lose_no_unicode():
    short = "<b>Привет</b> &amp; 👋"
    assert _pages(short, photo=True) == [short]
    visible = "🧑‍🚀 <люди> & текст\n" * 1000
    html = "<b>" + visible.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") + "</b>"
    for photo, limit in ((False, 3700), (True, 900)):
        pages = _pages(html, photo=photo)
        assert len(pages) > 1
        assert "".join(unescape(page) for page in pages) == visible
        assert all(_units(unescape(page)) <= limit for page in pages)


def test_text_edit_is_restart_stable_and_deletes_input_after_render(database_url):
    async def scenario(sessions):
        bot = FakeBot()
        first = await ScreenManager(sessions, bot).show(CHAT, "one")
        second = await ScreenManager(sessions, bot).show(
            CHAT, "two", trigger=message(50, sender=CHAT)
        )
        assert first == second
        assert [name for name, _ in bot.calls] == ["send_text", "edit_text", "delete"]
        assert bot.calls[-1][1]["message_id"] == 50
        saved = await stored(sessions)
        assert saved.message_id == first and saved.text == "two"

    run(database_url, scenario)


def test_notification_replaces_old_screen_and_sends_a_new_message(database_url):
    async def scenario(sessions):
        bot = FakeBot()
        first = await ScreenManager(sessions, bot).show(CHAT, "Menu")
        second = await ScreenManager(sessions, bot).show(CHAT, "New event", force_new=True)
        assert second != first
        assert [method for method, _ in bot.calls] == ["send_text", "send_text", "delete"]
        assert bot.calls[-1][1]["message_id"] == first
        assert (await stored(sessions)).message_id == second

    run(database_url, scenario)


def test_photo_changes_use_media_edit_and_custom_cover_does_not_poison_cache(database_url):
    async def scenario(sessions):
        bot, cache = FakeBot(), {}
        manager = ScreenManager(sessions, bot, file_ids=cache)
        first = await manager.show(CHAT, "menu", photo=legacy.banner("menu"), banner_role="menu")
        assert bot.calls[0][1]["photo"].data == legacy.banner("menu").data
        second = await ScreenManager(sessions, bot).show(
            CHAT, "new menu", photo=legacy.banner("menu"), banner_role="menu"
        )
        assert first == second
        assert bot.calls[-1][0] == "edit_photo"
        assert bot.calls[-1][1]["media"].media == "photo-file-id"
        await manager.show(CHAT, "custom", photo="my-custom-cover", banner_role="menu")
        assert bot.calls[-1][1]["media"].media == "my-custom-cover"
        fingerprint = hashlib.sha256(legacy.banner("menu").data).hexdigest()
        assert cache == {(BOT, f"menu:{fingerprint}"): "photo-file-id"}
        saved = await stored(sessions)
        assert saved.banner_role is None and saved.banner_cache == {
            "menu": {"sha256": fingerprint, "file_id": "photo-file-id"}
        }

    run(database_url, scenario)


def test_incompatible_kind_sends_replacement_before_deleting_old(database_url):
    async def scenario(sessions):
        bot = FakeBot()
        manager = ScreenManager(sessions, bot)
        first = await manager.show(CHAT, "text")
        photo = await manager.show(CHAT, "photo", photo="custom")
        final = await manager.show(CHAT, "text again")
        assert len({first, photo, final}) == 3
        assert [method for method, _ in bot.calls] == [
            "send_text",
            "send_photo",
            "delete",
            "send_text",
            "delete",
        ]
        assert bot.calls[2][1]["message_id"] == first
        assert bot.calls[4][1]["message_id"] == photo

    run(database_url, scenario)


def test_definitely_missing_message_falls_back_and_not_modified_does_not_duplicate(database_url):
    async def scenario(sessions):
        bot, method = FakeBot(), SendMessage(chat_id=CHAT, text="test")
        manager = ScreenManager(sessions, bot)
        first = await manager.show(CHAT, "one")
        bot.failure = (
            "edit_text",
            TelegramBadRequest(method=method, message="message is not modified"),
        )
        assert await manager.show(CHAT, "one") == first
        assert [name for name, _ in bot.calls] == ["send_text", "edit_text"]
        bot.failure = (
            "edit_text",
            TelegramBadRequest(method=method, message="message to edit not found"),
        )
        second = await manager.show(CHAT, "two")
        assert second != first
        assert [name for name, _ in bot.calls][-3:] == ["edit_text", "send_text", "delete"]

    run(database_url, scenario)


@pytest.mark.parametrize("error_type", [TelegramNetworkError, TelegramForbiddenError])
def test_uncertain_or_forbidden_edit_preserves_old_pointer_and_user_input(database_url, error_type):
    async def scenario(sessions):
        bot = FakeBot()
        manager = ScreenManager(sessions, bot)
        first = await manager.show(CHAT, "old")
        bot.failure = (
            "edit_text",
            error_type(method=SendMessage(chat_id=CHAT, text="x"), message="failure"),
        )
        with pytest.raises(error_type):
            await manager.show(CHAT, "new", trigger=message(40, sender=CHAT))
        saved = await stored(sessions)
        assert saved.message_id == first and saved.text == "old"
        assert [name for name, _ in bot.calls] == ["send_text", "edit_text"]

    run(database_url, scenario)


def test_old_own_callback_is_cleaned_up_but_foreign_message_is_untouched(database_url):
    async def scenario(sessions):
        bot = FakeBot()
        manager = ScreenManager(sessions, bot)
        current = await manager.show(CHAT, "one")
        await manager.show(CHAT, "two", trigger=callback(20))
        assert bot.calls[-1] == ("delete", {"chat_id": CHAT, "message_id": 20})
        bot.calls.clear()
        await manager.show(CHAT, "three", trigger=callback(30, sender=9009))
        assert [name for name, _ in bot.calls] == ["edit_text"]
        bot.calls.clear()
        await manager.show(CHAT, "four", trigger=callback(current))
        assert [name for name, _ in bot.calls] == ["edit_text"]

    run(database_url, scenario)


def test_long_photo_card_remains_one_message_and_pagination_survives_restart(database_url):
    async def scenario(sessions):
        bot = FakeBot()
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Back", callback_data="menu")]]
        )
        current = await ScreenManager(sessions, bot).show(
            CHAT, "<b>" + "Длинный текст 👋 &amp; " * 200 + "</b>", markup, "custom-photo"
        )
        assert [name for name, _ in bot.calls] == ["send_photo"]
        saved = await stored(sessions)
        assert len(saved.pages) > 1
        manager = ScreenManager(sessions, bot)
        assert await manager.handle_page(
            callback(current, data=f"screen:page:{saved.page_token}:1")
        )
        assert bot.calls[-1][0] == "edit_photo"
        assert _units(unescape(bot.calls[-1][1]["media"].caption)) <= 1024
        assert bot.calls[-1][1]["reply_markup"].inline_keyboard[0][0].callback_data == "menu"
        assert (await stored(sessions)).page_index == 1
        count = len(bot.calls)
        assert await manager.handle_page(callback(current, data="screen:page:stale:0"))
        assert len(bot.calls) == count

    run(database_url, scenario)


def test_two_managers_serialize_first_render_with_database_lock(database_url):
    async def scenario(sessions):
        bot = FakeBot()
        results = await asyncio.gather(
            *(ScreenManager(sessions, bot).show(CHAT, f"Screen {index}") for index in range(5))
        )
        assert len(set(results)) == 1
        assert [name for name, _ in bot.calls].count("send_text") == 1
        assert [name for name, _ in bot.calls].count("edit_text") == 4
        async with sessions() as session:
            assert len((await session.scalars(select(UIScreen))).all()) == 1

    run(database_url, scenario)


def test_banner_file_cache_is_scoped_to_bot_and_role(database_url):
    async def scenario(sessions):
        cache = {(BOT, "menu"): "cached-for-first-bot"}
        other = FakeBot(BOT + 1)
        await ScreenManager(sessions, other, file_ids=cache).show(
            CHAT, "menu", photo=legacy.banner("menu"), banner_role="menu"
        )
        assert other.calls[0][1]["photo"].data == legacy.banner("menu").data
        assert cache[(BOT, "menu")] == "cached-for-first-bot"
        fingerprint = hashlib.sha256(legacy.banner("menu").data).hexdigest()
        assert cache[(BOT + 1, f"menu:{fingerprint}")] == "photo-file-id"
        assert (await stored(sessions, BOT + 1)).bot_id == BOT + 1

    run(database_url, scenario)


def test_changed_original_banner_bytes_invalidate_persistent_and_memory_cache(
    database_url, monkeypatch
):
    async def scenario(sessions):
        bot, cache = FakeBot(), {}
        manager = ScreenManager(sessions, bot, file_ids=cache)
        await manager.show(CHAT, "original", photo=legacy.banner("menu"), banner_role="menu")
        old = (await stored(sessions)).banner_cache["menu"]["sha256"]
        monkeypatch.setattr(
            legacy,
            "banner",
            lambda role: BufferedInputFile(b"new-original-bytes", filename="menu.png"),
        )
        await manager.show(CHAT, "updated", photo=legacy.banner("menu"), banner_role="menu")
        assert bot.calls[-1][0] == "edit_photo"
        assert bot.calls[-1][1]["media"].media.data == b"new-original-bytes"
        updated = (await stored(sessions)).banner_cache["menu"]["sha256"]
        assert updated != old
        assert updated == hashlib.sha256(b"new-original-bytes").hexdigest()

    run(database_url, scenario)


@pytest.mark.parametrize("sender", [BOT, BOT + 100])
def test_first_callback_can_adopt_only_this_bots_message(database_url, sender):
    async def scenario(sessions):
        bot = FakeBot()
        shown = await ScreenManager(sessions, bot).show(
            CHAT, "new", trigger=callback(55, sender=sender)
        )
        if sender == BOT:
            assert shown == 55
            assert [name for name, _ in bot.calls] == ["edit_text"]
        else:
            assert shown != 55
            assert [name for name, _ in bot.calls] == ["send_text"]

    run(database_url, scenario)


def test_failed_replacement_does_not_delete_old_screen(database_url):
    async def scenario(sessions):
        bot = FakeBot()
        manager = ScreenManager(sessions, bot)
        first = await manager.show(CHAT, "old text")
        bot.failure = (
            "send_photo",
            TelegramNetworkError(
                method=SendMessage(chat_id=CHAT, text="x"), message="unknown outcome"
            ),
        )
        with pytest.raises(TelegramNetworkError):
            await manager.show(
                CHAT, "replacement", photo="custom", trigger=message(10, sender=CHAT)
            )
        saved = await stored(sessions)
        assert saved.message_id == first and saved.kind == "text"
        assert [name for name, _ in bot.calls] == ["send_text", "send_photo"]

    run(database_url, scenario)


def test_not_modified_custom_upload_keeps_photo_id_for_durable_pagination(database_url):
    async def scenario(sessions):
        bot = FakeBot()
        manager = ScreenManager(sessions, bot)
        await manager.show(CHAT, "custom", photo=BufferedInputFile(b"cover", filename="cover.png"))
        bot.failure = (
            "edit_photo",
            TelegramBadRequest(
                method=SendMessage(chat_id=CHAT, text="x"), message="message is not modified"
            ),
        )
        await manager.show(CHAT, "custom", photo=BufferedInputFile(b"cover", filename="cover.png"))
        assert (await stored(sessions)).photo_file_id == "photo-file-id"

    run(database_url, scenario)
