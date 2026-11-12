"""Durable runtime checks against disposable PostgreSQL and a strictly offline Telegram bot."""

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import AnswerCallbackQuery, SendDocument, SendMessage
from aiogram.types import Update
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

from cu_clubs import worker
from cu_clubs.app import create_app
from cu_clubs.config import Settings
from cu_clubs.db import make_database
from cu_clubs.models import Outbox, TelegramUpdate
from cu_clubs.services import ClubService
from cu_clubs.telegram import build_dispatcher

pytestmark = pytest.mark.integration
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADMIN, LEADER, OTHER = 201, 202, 203
WEBHOOK_SECRET = "offline-test-webhook-secret-32-characters"
SECRET_MARKER = "DO_NOT_EXPOSE_SYNTHETIC_CREDENTIAL"


@pytest.fixture
def runtime_database_url():
    configured = os.environ.get("TEST_DATABASE_URL")
    if not configured:
        pytest.skip("Set TEST_DATABASE_URL for PostgreSQL runtime acceptance checks")
    schema = f"test_runtime_{uuid4().hex}"
    engine = create_engine(configured)
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config = Config(str(PROJECT_ROOT / "alembic.ini"))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        scoped = make_url(configured).update_query_dict({"options": f"-csearch_path={schema}"})
        yield scoped.render_as_string(hide_password=False)
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


def settings_for(database_url, **overrides):
    values = {
        "_env_file": None,
        "environment": "test",
        "database_url": SecretStr(database_url),
        "external_delivery_enabled": True,
        "telegram_bot_token": SecretStr("123456:OFFLINE_RUNTIME_TEST_TOKEN"),
        "telegram_bot_username": "offline_test_bot",
        "telegram_webhook_secret": SecretStr(WEBHOOK_SECRET),
        "backup_recipient_telegram_id": LEADER,
    }
    values.update(overrides)
    return Settings(**values)


def run(database_url, scenario):
    async def exercise():
        settings = settings_for(database_url)
        engine, sessions = make_database(settings)
        try:
            await scenario(sessions, settings)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


class FakeBot(Bot):
    def __init__(self, error=None):
        super().__init__("123456:OFFLINE_RUNTIME_TEST_TOKEN")
        self.sent = []
        self.answered = []
        self.error = error

    async def __call__(self, method, request_timeout=None):
        if isinstance(method, AnswerCallbackQuery):
            self.answered.append(method)
            return True
        assert isinstance(method, SendMessage | SendDocument), type(method)
        if self.error:
            raise self.error
        self.sent.append(method)
        return SimpleNamespace(message_id=900 + len(self.sent))


async def enqueue(sessions, *, kind="club.created", recipient=LEADER, **values):
    async with sessions.begin() as session:
        row = Outbox(
            kind=kind,
            recipient_telegram_id=recipient,
            payload={"public_id": "cinema"},
            dedupe_key=uuid4().hex,
            **values,
        )
        session.add(row)
        await session.flush()
        return row.id


def test_webhook_auth_and_validation_happen_before_database(runtime_database_url):
    settings = settings_for(runtime_database_url)
    app = create_app(settings)

    def unexpected_database_call():
        pytest.fail("Rejected webhook must never open a database session")

    with TestClient(app) as client:
        app.state.sessions = unexpected_database_call
        for headers in ({}, {"X-Telegram-Bot-Api-Secret-Token": "wrong"}):
            response = client.post("/telegram/webhook", content=b"invalid json", headers=headers)
            assert response.status_code == 403
        headers = {"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET}
        assert (
            client.post("/telegram/webhook", content=b"invalid json", headers=headers).status_code
            == 400
        )
        for payload in ([], {}, {"update_id": True}, {"update_id": -1}, {"update_id": "1"}):
            assert (
                client.post("/telegram/webhook", json=payload, headers=headers).status_code == 422
            )
        response = client.post(
            "/telegram/webhook", content=b"x" * (1024 * 1024 + 1), headers=headers
        )
        assert response.status_code == 413


@pytest.mark.parametrize("transport", ["webhook", "polling"])
def test_webhook_ack_commits_one_durable_update_and_duplicate_preserves_first(
    runtime_database_url,
    transport,
):
    payload = {"update_id": 505, "message": {"text": "first payload"}}
    headers = {"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET}
    with TestClient(
        create_app(
            settings_for(
                runtime_database_url,
                telegram_transport=transport,
            )
        )
    ) as client:
        response = client.post("/telegram/webhook", json=payload, headers=headers)
        assert response.status_code == 200 and response.json() == {"ok": True}
        response = client.post(
            "/telegram/webhook", json={"update_id": 505, "edited": True}, headers=headers
        )
        assert response.status_code == 200

    async def scenario(sessions, settings):
        async with sessions.begin() as session:
            assert await session.scalar(select(func.count()).select_from(TelegramUpdate)) == 1
            row = await session.get(TelegramUpdate, 505)
            assert row.payload == payload
            assert row.status == "pending" and row.attempts == 0

    run(runtime_database_url, scenario)


def test_inbox_claim_reclaims_expired_lease_and_retries_with_safe_error(runtime_database_url):
    async def scenario(sessions, settings):
        now = datetime.now(UTC)
        async with sessions.begin() as session:
            session.add_all(
                [
                    TelegramUpdate(
                        update_id=1,
                        payload={"update_id": 1},
                        status="processing",
                        attempts=1,
                        lease_until=now - timedelta(seconds=1),
                    ),
                    TelegramUpdate(
                        update_id=2,
                        payload={"update_id": 2},
                        status="processing",
                        attempts=1,
                        lease_until=now + timedelta(hours=1),
                    ),
                    TelegramUpdate(
                        update_id=3,
                        payload={"update_id": 3},
                        status="pending",
                        available_at=now + timedelta(hours=1),
                    ),
                ]
            )
        item = await worker.claim_inbox(sessions)
        assert item["update_id"] == 1 and item["attempts"] == 2
        assert await worker.claim_inbox(sessions) is None
        await worker.finish_inbox(sessions, item, RuntimeError(SECRET_MARKER))
        async with sessions.begin() as session:
            row = await session.get(TelegramUpdate, 1)
            assert row.status == "pending" and row.lease_until is None
            assert row.last_error == "RuntimeError"
            assert row.available_at > now
            row.available_at = now - timedelta(seconds=1)
            row.attempts = worker.MAX_ATTEMPTS - 1
        item = await worker.claim_inbox(sessions)
        assert item["attempts"] == worker.MAX_ATTEMPTS
        await worker.finish_inbox(sessions, item, RuntimeError(SECRET_MARKER))
        async with sessions.begin() as session:
            assert (await session.get(TelegramUpdate, 1)).status == "failed"
            row = await session.get(TelegramUpdate, 3)
            row.available_at = now - timedelta(seconds=1)
        last = await worker.claim_inbox(sessions)
        assert last["update_id"] == 3
        await worker.finish_inbox(sessions, last)
        async with sessions.begin() as session:
            row = await session.get(TelegramUpdate, 3)
            assert row.status == "done" and row.lease_until is None and row.last_error is None

    run(runtime_database_url, scenario)


def test_outbox_expired_delivery_becomes_uncertain_without_resending(runtime_database_url):
    async def scenario(sessions, settings):
        now = datetime.now(UTC)
        expired = await enqueue(
            sessions, status="processing", attempts=1, lease_until=now - timedelta(seconds=1)
        )
        active = await enqueue(
            sessions, status="processing", attempts=1, lease_until=now + timedelta(hours=1)
        )
        pending = await enqueue(sessions)
        item = await worker.claim_outbox(sessions)
        assert item["id"] == pending and item["attempts"] == 1
        assert await worker.claim_outbox(sessions) is None
        async with sessions.begin() as session:
            row = await session.get(Outbox, expired)
            assert row.status == "uncertain" and row.last_error == "ExpiredDeliveryLease"
            assert row.lease_until is None and row.attempts == 1
            assert (await session.get(Outbox, active)).status == "processing"

    run(runtime_database_url, scenario)


def test_bootstrap_notification_is_delivered_once_and_card_button_works(runtime_database_url):
    async def scenario(sessions, settings):
        async with sessions.begin() as session:
            await ClubService(session).bootstrap([ADMIN], LEADER)
        bot = FakeBot()
        try:
            item = await worker.claim_outbox(sessions)
            assert item["kind"] == "club.created" and item["recipient"] == LEADER
            await worker.process_outbox(sessions, bot, item, settings)
            assert len(bot.sent) == 1
            message = bot.sent[0]
            assert message.chat_id == LEADER and "создан" in message.text
            buttons = [button for row in message.reply_markup.inline_keyboard for button in row]
            assert any(
                button.url == "https://t.me/offline_test_bot?start=c_cinema" for button in buttons
            )
            assert any(button.callback_data == "manageclub:cinema" for button in buttons)
            async with sessions.begin() as session:
                row = await session.get(Outbox, item["id"])
                assert row.status == "sent" and row.delivered_at is not None
                assert row.telegram_message_id == 901 and row.lease_until is None
                assert row.last_error is None
            assert await worker.claim_outbox(sessions) is None
            update = Update.model_validate(
                {
                    "update_id": 606,
                    "callback_query": {
                        "id": "open-created-club",
                        "chat_instance": "test",
                        "from": {"id": LEADER, "first_name": "Leader", "is_bot": False},
                        "message": {
                            "message_id": 901,
                            "date": datetime.now(UTC),
                            "chat": {"id": LEADER, "type": "private"},
                        },
                        "data": "manageclub:cinema",
                    },
                }
            )
            dispatcher = build_dispatcher(sessions, settings)
            await dispatcher.feed_update(bot, update)
            assert "Управление" in bot.sent[-1].text
            assert any(
                button.callback_data == "editmenu:cinema"
                for row in bot.sent[-1].reply_markup.inline_keyboard
                for button in row
            )
            assert bot.answered
        finally:
            await bot.session.close()

    run(runtime_database_url, scenario)


def test_backup_delivery_rejects_wrong_recipient_and_unsafe_filename(
    runtime_database_url, tmp_path
):
    async def scenario(sessions, settings):
        settings.backup_directory = tmp_path
        bot = FakeBot()
        try:
            wrong = await enqueue(sessions, kind="backup.ready", recipient=OTHER)
            item = await worker.claim_outbox(sessions)
            assert item["id"] == wrong
            await worker.process_outbox(sessions, bot, item, settings)
            async with sessions.begin() as session:
                row = await session.get(Outbox, wrong)
                assert row.status == "failed" and row.last_error == "ValueError"
            assert not bot.sent
            for filename in ("../private.dump", "/tmp/private.dump", "not-a-dump.txt"):
                with pytest.raises(ValueError, match="filename"):
                    await worker.deliver(
                        bot,
                        {
                            "kind": "backup.ready",
                            "recipient": LEADER,
                            "payload": {"filename": filename},
                        },
                        settings,
                    )
            backup = tmp_path / "safe.dump"
            backup.write_bytes(b"synthetic backup payload")
            result = await worker.deliver(
                bot,
                {
                    "kind": "backup.ready",
                    "recipient": LEADER,
                    "payload": {"filename": "safe.dump", "date": "2026-01-01"},
                },
                settings,
            )
            assert result.message_id == 901
            assert isinstance(bot.sent[0], SendDocument)
            assert bot.sent[0].chat_id == LEADER
            assert Path(bot.sent[0].document.path) == backup
        finally:
            await bot.session.close()

    run(runtime_database_url, scenario)


@pytest.mark.parametrize(
    "error_kind, expected_status",
    [
        ("network", "uncertain"),
        ("forbidden", "failed"),
        ("retry", "pending"),
        ("unexpected", "failed"),
    ],
)
def test_outbox_error_status_does_not_expose_exception_text(
    runtime_database_url,
    caplog,
    error_kind,
    expected_status,
):
    caplog.set_level(logging.INFO, logger="cu_clubs.worker")

    async def scenario(sessions, settings):
        method = SendMessage(chat_id=LEADER, text="test")
        failures = {
            "network": TelegramNetworkError(method=method, message=SECRET_MARKER),
            "forbidden": TelegramForbiddenError(method=method, message=SECRET_MARKER),
            "retry": TelegramRetryAfter(method=method, message=SECRET_MARKER, retry_after=10),
            "unexpected": RuntimeError(SECRET_MARKER),
        }
        bot = FakeBot(failures[error_kind])
        try:
            row_id = await enqueue(sessions)
            item = await worker.claim_outbox(sessions)
            await worker.process_outbox(sessions, bot, item, settings)
            assert not bot.sent
            async with sessions.begin() as session:
                row = await session.get(Outbox, row_id)
                assert row.status == expected_status
                assert row.last_error == type(failures[error_kind]).__name__
                assert row.lease_until is None
                if expected_status == "pending":
                    assert row.available_at > datetime.now(UTC)
            assert SECRET_MARKER not in caplog.text
        finally:
            await bot.session.close()

    run(runtime_database_url, scenario)


def test_expired_inbox_and_outbox_claim_cannot_overwrite_new_attempt(runtime_database_url):
    async def scenario(sessions, settings):
        async with sessions.begin() as session:
            session.add(TelegramUpdate(update_id=707, payload={"update_id": 707}))
        stale = await worker.claim_inbox(sessions)
        async with sessions.begin() as session:
            row = await session.get(TelegramUpdate, 707)
            row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        current = await worker.claim_inbox(sessions)
        assert current["attempts"] == stale["attempts"] + 1
        await worker.finish_inbox(sessions, stale, RuntimeError(SECRET_MARKER))
        async with sessions.begin() as session:
            row = await session.get(TelegramUpdate, 707)
            assert row.status == "processing" and row.attempts == current["attempts"]
            assert row.last_error is None
        await worker.finish_inbox(sessions, current)
        await worker.finish_inbox(sessions, stale, RuntimeError(SECRET_MARKER))
        async with sessions.begin() as session:
            assert (await session.get(TelegramUpdate, 707)).status == "done"

        outbox_id = await enqueue(sessions)
        stale_outbox = await worker.claim_outbox(sessions)
        async with sessions.begin() as session:
            row = await session.get(Outbox, outbox_id)
            # Model an explicitly approved requeue after the old worker's lease.
            row.status = "pending"
            row.lease_until = None
        current_outbox = await worker.claim_outbox(sessions)
        failing = FakeBot(RuntimeError(SECRET_MARKER))
        successful = FakeBot()
        try:
            await worker.process_outbox(sessions, failing, stale_outbox, settings)
            async with sessions.begin() as session:
                row = await session.get(Outbox, outbox_id)
                assert row.status == "processing"
                assert row.attempts == current_outbox["attempts"]
                assert row.last_error is None
            await worker.process_outbox(sessions, successful, current_outbox, settings)
            await worker.process_outbox(sessions, failing, stale_outbox, settings)
            async with sessions.begin() as session:
                row = await session.get(Outbox, outbox_id)
                assert row.status == "sent" and row.telegram_message_id == 901
                assert row.last_error is None
        finally:
            await failing.session.close()
            await successful.session.close()

    run(runtime_database_url, scenario)


def test_polling_acknowledges_after_commit_and_restart_deduplicates(runtime_database_url):
    def update(update_id, message_text):
        return Update.model_validate(
            {
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "date": datetime(2026, 1, 1, tzinfo=UTC),
                    "chat": {"id": LEADER, "type": "private"},
                    "from": {"id": LEADER, "is_bot": False, "first_name": "Leader"},
                    "text": message_text,
                },
            }
        )

    async def scenario(sessions, settings):
        class PollingBot:
            def __init__(self, batch, initial_offset, next_offset, expected_rows):
                self.batch = batch
                self.initial_offset = initial_offset
                self.next_offset = next_offset
                self.expected_rows = expected_rows
                self.calls = 0

            async def get_updates(self, *, offset, timeout, allowed_updates):
                self.calls += 1
                assert timeout == 25
                assert allowed_updates == ["message", "callback_query"]
                if self.calls == 1:
                    assert offset == self.initial_offset
                    return self.batch
                assert self.calls == 2 and offset == self.next_offset
                # An independent session must see the entire committed batch before
                # the next request acknowledges its last update to Telegram.
                async with sessions.begin() as session:
                    rows = list(
                        await session.scalars(
                            select(TelegramUpdate).order_by(TelegramUpdate.update_id)
                        )
                    )
                    assert len(rows) == self.expected_rows
                    assert rows[-1].update_id + 1 == offset
                    assert all(row.status == "pending" and row.attempts == 0 for row in rows)
                raise asyncio.CancelledError

        first = PollingBot(
            [
                update(901, "original 901"),
                update(900, "original 900"),
                update(900, "duplicate 900"),
            ],
            initial_offset=None,
            next_offset=902,
            expected_rows=2,
        )
        with pytest.raises(asyncio.CancelledError):
            await worker.poll_updates(first, sessions)
        assert first.calls == 2
        restarted = PollingBot(
            [update(901, "redelivered 901"), update(902, "new 902")],
            initial_offset=902,
            next_offset=903,
            expected_rows=3,
        )
        with pytest.raises(asyncio.CancelledError):
            await worker.poll_updates(restarted, sessions)
        assert restarted.calls == 2
        async with sessions.begin() as session:
            assert (await session.get(TelegramUpdate, 900)).payload["message"]["text"] == (
                "original 900"
            )
            assert (await session.get(TelegramUpdate, 901)).payload["message"]["text"] == (
                "original 901"
            )
            assert (await session.get(TelegramUpdate, 902)).payload["message"]["text"] == "new 902"

    run(runtime_database_url, scenario)
