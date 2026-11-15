"""Real PG checks for public-only snapshots, durable selection and revalidation."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_events import ADMIN, LEADER, published, seed
from test_events import sessions as sessions

from cu_clubs.digest import DigestService, week_bounds
from cu_clubs.digest_ui import handle_digest
from cu_clubs.event_models import Event, EventClub
from cu_clubs.models import Club, Conversation
from cu_clubs.services import PermissionDenied, ValidationError

SETTINGS = SimpleNamespace(telegram_bot_username="new_cu_clubs_bot")


def test_public_snapshot_and_revalidation(sessions):
    async def scenario():
        await seed(sessions)
        first = await published(sessions)
        hidden = await published(sessions)
        async with sessions.begin() as session:
            event = await session.scalar(select(Event).where(Event.public_id == first["public_id"]))
            other = await session.scalar(
                select(Event).where(Event.public_id == hidden["public_id"])
            )
            other.visibility = "closed"
            partner = await session.scalar(select(Club).where(Club.public_id == "p" * 32))
            session.add(EventClub(event_id=event.id, club_id=partner.id, role="partner"))
            await session.flush()
            week = week_bounds(event.starts_at.date().isoformat())[0].isoformat()
            service = DigestService(session, SETTINGS)
            events = await service.candidates(ADMIN, week)
            assert [e.public_id for e in events] == [event.public_id]
            selection = {event.public_id: event.version}
            encoded, _ = await service.snapshot(ADMIN, week, selection)
            snapshot = json.loads(encoded)
            assert len(snapshot["events"]) == 1
            item = snapshot["events"][0]
            assert len(item["clubs"]) == 2 and item["starts_at"].endswith("+03:00")
            assert "Секрет" not in encoded.decode() and "person" not in encoded.decode()
            assert item["public_url"].endswith("?start=e_" + event.public_id)
            event.version += 1
            await session.flush()
            assert (await service.snapshot(ADMIN, week, selection))[0] is None
            with pytest.raises(PermissionDenied):
                await service.snapshot(LEADER, week, selection)
            event.description = "x" * 1_000_001
            await session.flush()
            with pytest.raises(ValidationError, match="1 МБ"):
                await service.snapshot(ADMIN, week, {event.public_id: event.version})

    asyncio.run(scenario())


def test_ui_selection_nonce_preview_export(sessions):
    async def scenario():
        await seed(sessions)
        first = await published(sessions)
        async with sessions.begin() as session:
            event = await session.scalar(select(Event).where(Event.public_id == first["public_id"]))
            state = Conversation(telegram_id=ADMIN, state="idle", payload={})
            session.add(state)

            async def act(action):
                return await handle_digest(session, ADMIN, state, None, action, SETTINGS)

            await act("digest")

            def prefix():
                return "dg:" + state.payload["nonce"] + ":"

            await act(prefix() + "week:" + event.starts_at.date().isoformat())
            await act(prefix() + "all")
            assert state.payload["selected"] == {event.public_id: event.version}
            old = prefix() + "export"
            await act(prefix() + "manual:0")
            with pytest.raises(ValidationError, match="устарела"):
                await act(old)
            await act(prefix() + "preview")
            event.title = "Changed"
            event.version += 1
            await session.flush()
            refreshed = await act(prefix() + "export")
            assert refreshed.document is None and "изменились" in refreshed.text
            exported = await act(prefix() + "export")
            assert json.loads(exported.document.data)["events"][0]["title"] == "Changed"
            event.starts_at += timedelta(days=8)
            event.ends_at += timedelta(days=8)
            await session.flush()
            refreshed = await act(prefix() + "export")
            assert refreshed.document is None and state.payload["selected"] == {}

    asyncio.run(scenario())


def test_document_removed_on_navigation(sessions):
    from aiogram.types import BufferedInputFile
    from test_screens import CHAT, FakeBot

    from cu_clubs.screens import ScreenManager, UIScreen

    class DocumentsBot(FakeBot):
        async def send_document(self, **kwargs):
            return await self._result("send_document", kwargs)

    async def scenario():
        bot = DocumentsBot()
        manager = ScreenManager(sessions, bot)
        await manager.show(CHAT, "Preview")
        await manager.send_document(CHAT, BufferedInputFile(b"{}", filename="digest.json"))
        async with sessions() as session:
            screen = await session.get(UIScreen, (bot.id, CHAT))
            file_message = screen.banner_cache["ui_document_message_id"]
        # A fresh manager models restart: the tracked file is durable, not process-local.
        await ScreenManager(sessions, bot).show(CHAT, "Admin menu")
        assert any(
            call == "delete" and args["message_id"] == file_message for call, args in bot.calls
        )
        async with sessions() as session:
            screen = await session.get(UIScreen, (bot.id, CHAT))
            assert "ui_document_message_id" not in screen.banner_cache

    asyncio.run(scenario())


def test_all_mode_additions_and_back_retention(sessions):
    async def scenario():
        await seed(sessions)
        first = await published(sessions)
        async with sessions.begin() as session:
            event = await session.scalar(select(Event).where(Event.public_id == first["public_id"]))
            state = Conversation(telegram_id=ADMIN, state="idle", payload={})
            session.add(state)

            async def action(value):
                data = (
                    "digest" if value == "digest" else "dg:" + state.payload["nonce"] + ":" + value
                )
                return await handle_digest(session, ADMIN, state, None, data, SETTINGS)

            await action("digest")
            await action("week:" + event.starts_at.date().isoformat())
            await action("all")
            expected = dict(state.payload["selected"])
            await action("manual:0")
            await action("mode")
            assert state.payload["selected"] == expected
            await action("all")
            # A new eligible event in the same week invalidates the whole all-mode snapshot.
            from test_events import fields

            from cu_clubs.events import EventService

            service = EventService(session)
            draft = await service.create_draft(
                LEADER, "cinema", fields(starts_at=event.starts_at, ends_at=event.ends_at)
            )
            await service.publish(LEADER, draft["public_id"], draft["version"])
            result = await action("export")
            assert result.document is None and "изменились" in result.text
            assert len(state.payload["selected"]) == 2

    asyncio.run(scenario())


def test_limit_errors_keep_digest_context(sessions, monkeypatch):
    from cu_clubs import digest, digest_ui

    # Exercise the real limit branches with a small synthetic week.
    monkeypatch.setattr(digest, "MAX_EVENTS", 2)
    monkeypatch.setattr(digest_ui, "MAX_EVENTS", 2)

    async def scenario():
        await seed(sessions)
        items = [await published(sessions) for _ in range(3)]
        async with sessions.begin() as session:
            events = list(await session.scalars(select(Event).order_by(Event.starts_at)))
            state = Conversation(telegram_id=ADMIN, state="idle", payload={})
            session.add(state)

            async def action(value):
                data = (
                    "digest" if value == "digest" else "dg:" + state.payload["nonce"] + ":" + value
                )
                return await handle_digest(session, ADMIN, state, None, data, SETTINGS)

            await action("digest")
            await action("week:" + events[0].starts_at.date().isoformat())
            result = await action("all")
            assert "500" in result.text and state.payload["view"] == "mode"
            assert any(
                b.text == "Выбрать вручную" for row in result.markup.inline_keyboard for b in row
            )
            await action("manual:0")
            for event in events[:2]:
                await action("toggle:" + event.public_id + ":0")
            before = dict(state.payload["selected"])
            result = await action("toggle:" + events[2].public_id + ":0")
            assert "Сними другую галочку" in result.text
            assert state.payload["selected"] == before and state.payload["view"] == "manual"
            assert any(
                b.text.startswith("✓ ") for row in result.markup.inline_keyboard for b in row
            )
            await action("preview")
            monkeypatch.setattr(digest, "MAX_BYTES", 10)
            result = await action("export")
            assert result.document is None and "1 МБ" in result.text
            assert state.payload["view"] == "preview" and state.payload["selected"] == before
            assert any(
                b.text == "Изменить выбор" for row in result.markup.inline_keyboard for b in row
            )
            monkeypatch.setattr(digest, "MAX_BYTES", 1_000_000)
            events[0].ends_at = None
            await session.flush()
            result = await action("export")
            assert result.document is None and "времени окончания" in result.text
            assert state.payload["selected"] == before and state.payload["view"] == "preview"
            assert len(items) == 3

    asyncio.run(scenario())
