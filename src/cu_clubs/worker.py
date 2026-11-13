"""Durable Telegram inbox, outbox delivery, and daily PostgreSQL backups."""

import asyncio
import contextlib
import logging
import os
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Update
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url

from cu_clubs import legacy
from cu_clubs.config import Settings
from cu_clubs.db import make_database
from cu_clubs.identity import verification_delivery
from cu_clubs.inbox_crypto import open_update, seal_update
from cu_clubs.integrations.mail import send_verification_code
from cu_clubs.models import Outbox, TelegramUpdate
from cu_clubs.screens import ScreenManager
from cu_clubs.services import ClubService, PermissionDenied
from cu_clubs.telegram import build_dispatcher
from cu_clubs.ui_common import keyboard

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 8


def utcnow() -> datetime:
    return datetime.now(UTC)


async def poll_updates(bot: Bot, sessions, settings: Settings | None = None) -> None:
    """Test transport: commit received updates before acknowledging them to Telegram."""
    async with sessions() as session:
        latest = await session.scalar(select(func.max(TelegramUpdate.update_id)))
    offset = latest + 1 if latest is not None else None
    while True:
        try:
            updates = await bot.get_updates(
                offset=offset, timeout=25, allowed_updates=["message", "callback_query"]
            )
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
            continue
        except (TelegramNetworkError, TelegramServerError):
            logger.warning("Telegram polling connection interrupted")
            await asyncio.sleep(3)
            continue
        if not updates:
            continue
        async with sessions() as session, session.begin():
            await session.execute(
                insert(TelegramUpdate)
                .values(
                    [
                        {
                            "update_id": item.update_id,
                            "payload": seal_update(
                                item.model_dump(mode="json", exclude_none=True, exclude_unset=True),
                                settings.verification_secret if settings else None,
                            ),
                        }
                        for item in updates
                    ]
                )
                .on_conflict_do_nothing(index_elements=[TelegramUpdate.update_id])
            )
        offset = max(item.update_id for item in updates) + 1


async def claim_inbox(sessions) -> dict | None:
    now = utcnow()
    async with sessions() as session, session.begin():
        item = await session.scalar(
            select(TelegramUpdate)
            .where(
                or_(
                    and_(TelegramUpdate.status == "pending", TelegramUpdate.available_at <= now),
                    and_(TelegramUpdate.status == "processing", TelegramUpdate.lease_until <= now),
                )
            )
            .order_by(TelegramUpdate.update_id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if item is None:
            return None
        item.status = "processing"
        item.attempts += 1
        item.lease_until = now + timedelta(seconds=120)
        return {"update_id": item.update_id, "payload": item.payload, "attempts": item.attempts}


async def finish_inbox(sessions, item: dict, error: Exception | None = None) -> None:
    values = {"status": "done", "lease_until": None, "last_error": None}
    if error:
        values.update(
            status="failed" if item["attempts"] >= MAX_ATTEMPTS else "pending",
            last_error=type(error).__name__,
            available_at=utcnow() + timedelta(seconds=min(300, 2 ** item["attempts"])),
        )
    async with sessions() as session, session.begin():
        await session.execute(
            update(TelegramUpdate)
            .where(
                TelegramUpdate.update_id == item["update_id"],
                TelegramUpdate.status == "processing",
                TelegramUpdate.attempts == item["attempts"],
            )
            .values(**values)
        )


async def claim_outbox(sessions) -> dict | None:
    now = utcnow()
    async with sessions() as session, session.begin():
        # A crashed sender may already have delivered. Preserve the uncertain state
        # instead of silently sending the same notification again.
        await session.execute(
            update(Outbox)
            .where(Outbox.status == "processing", Outbox.lease_until <= now)
            .values(status="uncertain", lease_until=None, last_error="ExpiredDeliveryLease")
        )
        item = await session.scalar(
            select(Outbox)
            .where(Outbox.status == "pending", Outbox.available_at <= now)
            .order_by(Outbox.created_at, Outbox.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if item is None:
            return None
        item.status = "processing"
        item.attempts += 1
        item.lease_until = now + timedelta(seconds=180)
        return {
            "id": item.id,
            "kind": item.kind,
            "recipient": item.recipient_telegram_id,
            "payload": item.payload,
            "attempts": item.attempts,
        }


async def deliver(bot: Bot, item: dict, settings: Settings, *, sessions=None):
    if item["kind"] == "club.created":
        club = item["payload"]
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Открыть клуб",
                        callback_data=f"club:{club['public_id']}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Заполнить карточку", callback_data=f"manageclub:{club['public_id']}"
                    )
                ],
            ]
        )
        if sessions is None:
            raise ValueError("Club notification requires the durable screen manager")
        message_id = await ScreenManager(sessions, bot).show(
            item["recipient"],
            legacy.club_created_text(club["name"])
            + f"\n\nУровень — {club.get('level', 'Seed')}. Ты назначен лидером.\n\n"
            "Описание, обложку, ссылки и публичную команду можно заполнить в разделе "
            "«Мои клубы → Профиль». Публичная команда и права организаторов "
            "настраиваются отдельно.",
            markup=keyboard,
            force_new=True,
        )
        return SimpleNamespace(message_id=message_id)
    if item["kind"] == "backup.ready":
        # Only the configured owner receives database files, even if an outbox row is altered.
        if item["recipient"] != settings.backup_recipient_telegram_id:
            raise ValueError("Unexpected backup recipient")
        filename = item["payload"]["filename"]
        if Path(filename).name != filename or not filename.endswith(".dump"):
            raise ValueError("Invalid backup filename")
        path = settings.backup_directory / filename
        if path.stat().st_size > 49 * 1024 * 1024:
            raise ValueError("Backup exceeds configured transport budget")
        return await bot.send_document(
            item["recipient"],
            FSInputFile(path),
            caption=f"Резервная копия CU Clubs · {item['payload']['date']} · PostgreSQL",
        )
    raise ValueError("Unknown outbox delivery kind")


async def deliver_domain_notice(sessions, bot: Bot, item: dict, settings: Settings):
    kind, payload = item["kind"], item["payload"]
    if kind.startswith("event."):
        from cu_clubs.event_ui import event_text
        from cu_clubs.events import EventService

        async with sessions() as session:
            service = EventService(session)
            if kind == "event.invitation":
                event = next(
                    (
                        event
                        for event in await service.pending_invitations(item["recipient"])
                        if event["invitation_id"] == payload["invitation"]
                    ),
                    None,
                )
                if event is None:
                    return SimpleNamespace(message_id=None)
                heading = "Приглашение провести совместную встречу"
                markup = keyboard([("Открыть приглашение", f"evreview:{payload['invitation']}")])
            else:
                try:
                    event = await service.get_event(payload["event"], item["recipient"])
                except (PermissionDenied, ValueError):
                    return SimpleNamespace(message_id=None)
                if (
                    kind == "event.published"
                    and not event["can_manage"]
                    and not event["registered"]
                ):
                    subscriptions = [
                        await ClubService(session).get_subscription(
                            item["recipient"], club["public_id"]
                        )
                        for club in event["clubs"]
                    ]
                    if (
                        event["status"] != "published"
                        or event["visibility"] != "open"
                        or not any(subscriptions)
                    ):
                        return SimpleNamespace(message_id=None)
                heading = {
                    "event.published": "Новая встреча",
                    "event.changed": "Информация о встрече изменилась",
                    "event.cancelled": "Встреча отменена",
                    "event.partner_declined": "Партнёр отклонил приглашение",
                }[kind]
                markup = keyboard([("Открыть встречу", f"event:{payload['event']}:link")])
            text = f"<b>{heading}</b>\n\n" + event_text(event)
        role = "events"
    else:
        role = "club-owner"
        title = escape(payload["name"])
        level = escape(payload["level"])
        text = (
            f"Клуб: <b>{title}</b>\nУстановлен уровень: {level}"
            if kind == "club.level.changed"
            else f"Ты назначен лидером клуба <b>{title}</b>.\nУровень: {level}"
        )
        markup = keyboard([("Открыть клуб", f"manageclub:{payload['public_id']}")])
    message_id = await ScreenManager(sessions, bot).show(
        item["recipient"], text, markup, legacy.banner(role), banner_role=role, force_new=True
    )
    return SimpleNamespace(message_id=message_id)


async def process_outbox(sessions, bot: Bot, item: dict, settings: Settings) -> None:
    values = {"lease_until": None}
    try:
        if item["kind"] == "calendar.digest.notice":
            from cu_clubs.events import EventService

            async with sessions() as session:
                event = await EventService(session).get_event(
                    item["payload"]["event"], item["recipient"]
                )
                if not event["can_manage"]:
                    raise PermissionDenied("Calendar notice is no longer authorized")
                text = "Встреча выделена для дайджеста в календаре. Проверь анонс.\n\n" + escape(
                    event["title"]
                )
            msg = await ScreenManager(sessions, bot).show(
                item["recipient"],
                text,
                keyboard([("Открыть встречу", f"event:{event['public_id']}")]),
                legacy.banner("club-owner"),
                banner_role="club-owner",
                force_new=True,
            )
            result = SimpleNamespace(message_id=msg)
        elif item["kind"] == "email.verification":
            if not settings.verification_secret:
                raise ValueError("Email verification key is unavailable")
            async with sessions() as session:
                delivery = await verification_delivery(
                    session,
                    item["payload"]["challenge_id"],
                    settings.verification_secret.get_secret_value(),
                )
            if delivery:
                await send_verification_code(settings, delivery["email"], delivery["code"])
            result = SimpleNamespace(message_id=None)
        elif item["kind"].startswith(("event.", "club.level.", "club.leader.")):
            result = await deliver_domain_notice(sessions, bot, item, settings)
        else:
            result = await deliver(bot, item, settings, sessions=sessions)
        values.update(
            status="sent",
            delivered_at=utcnow(),
            telegram_message_id=result.message_id,
            last_error=None,
        )
    except TelegramRetryAfter as exc:
        values.update(
            status="pending",
            last_error="TelegramRetryAfter",
            available_at=utcnow() + timedelta(seconds=exc.retry_after + 1),
        )
    except TelegramNetworkError:
        values.update(status="uncertain", last_error="TelegramNetworkError")
    except TelegramForbiddenError:
        values.update(status="failed", last_error="TelegramForbiddenError")
    except TelegramAPIError as exc:
        values.update(status="failed", last_error=type(exc).__name__)
    except Exception as exc:
        values.update(status="failed", last_error=type(exc).__name__)
    async with sessions() as session, session.begin():
        await session.execute(
            update(Outbox)
            .where(
                Outbox.id == item["id"],
                Outbox.attempts == item["attempts"],
                Outbox.status.in_(["processing", "uncertain"]),
            )
            .values(**values)
        )
    logger.info("Outbox result: kind=%s status=%s", item["kind"], values["status"])


async def prepare_backup(sessions, settings: Settings, *, force: bool = False) -> bool:
    if not settings.backups_enabled:
        return False
    local = datetime.now(ZoneInfo(settings.timezone))
    # Initial deployment supports the documented daily schedule; reject other syntax.
    minute, hour, day, month, weekday = settings.backup_cron.split()
    if (day, month, weekday) != ("*", "*", "*"):
        raise ValueError("Only a daily backup schedule is supported")
    if not force and (local.hour, local.minute) < (int(hour), int(minute)):
        return False
    date = local.date().isoformat()
    dedupe = f"backup.ready:{date}"
    async with sessions() as session:
        if await session.scalar(select(Outbox.id).where(Outbox.dedupe_key == dedupe)):
            return False
    directory = settings.backup_directory
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    filename = f"cu-clubs-{date}.dump"
    path = directory / filename
    if not path.exists():
        url = make_url(settings.database_url.get_secret_value())
        environment = {
            **os.environ,
            "PGHOST": url.host or "localhost",
            "PGPORT": str(url.port or 5432),
            "PGUSER": url.username or "",
            "PGPASSWORD": url.password or "",
            "PGDATABASE": url.database or "",
        }
        partial = path.with_suffix(".dump.part")
        with partial.open("wb") as stream:
            partial.chmod(0o600)
            process = await asyncio.create_subprocess_exec(
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-acl",
                stdout=stream,
                stderr=asyncio.subprocess.DEVNULL,
                env=environment,
            )
            try:
                async with asyncio.timeout(120):
                    result = await process.wait()
            except TimeoutError:
                process.kill()
                await process.wait()
                raise RuntimeError("Database backup timed out") from None
        if result or not partial.stat().st_size:
            raise RuntimeError("Database backup failed")
        partial.replace(path)
    async with sessions() as session, session.begin():
        await session.execute(
            insert(Outbox)
            .values(
                kind="backup.ready",
                recipient_telegram_id=settings.backup_recipient_telegram_id,
                payload={"filename": filename, "date": date},
                dedupe_key=dedupe,
            )
            .on_conflict_do_nothing(index_elements=[Outbox.dedupe_key])
        )
    return True


async def calendar_loop(sessions, settings):
    from cu_clubs.calendar import CalendarSync

    sync = CalendarSync(sessions, settings)
    while True:
        try:
            await sync.tick(limit=settings.calendar_sync_batch_size)
        except Exception as error:
            logger.warning("Calendar synchronization failed: %s", type(error).__name__)
        await asyncio.sleep(settings.calendar_sync_interval_seconds)


async def run_worker(settings: Settings) -> None:
    if not settings.external_delivery_enabled:
        raise ValueError("Worker requires external delivery to be enabled explicitly")
    engine, sessions = make_database(settings)
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    dispatcher = build_dispatcher(sessions, settings)
    last_backup_check = 0.0
    polling = None
    calendar_task = None
    try:
        identity = await bot.get_me()
        if identity.username != settings.telegram_bot_username:
            raise ValueError("Telegram token belongs to a different bot")
        if settings.calendar_sync_enabled:
            calendar_task = asyncio.create_task(calendar_loop(sessions, settings))
        if settings.telegram_transport == "polling":
            webhook = await bot.get_webhook_info()
            if webhook.url:
                raise ValueError("Polling cannot start while a webhook is configured")
            polling = asyncio.create_task(poll_updates(bot, sessions, settings))
        while True:
            if polling is not None and polling.done():
                polling.result()
            Path("/tmp/cu-clubs-worker-heartbeat").touch()
            item = await claim_inbox(sessions)
            if item:
                try:
                    telegram_update = Update.model_validate(
                        open_update(item["payload"], settings.verification_secret),
                        context={"bot": bot},
                    )
                    await dispatcher.feed_update(
                        bot, telegram_update, inbox_attempt=item["attempts"]
                    )
                    message = telegram_update.message
                    if (
                        message
                        and message.from_user
                        and message.chat.type == "private"
                        and (message.text or "").startswith("/start")
                    ):
                        async with sessions() as session, session.begin():
                            await session.execute(
                                update(Outbox)
                                .where(
                                    Outbox.recipient_telegram_id == message.from_user.id,
                                    Outbox.kind == "club.created",
                                    Outbox.status == "failed",
                                    Outbox.last_error == "TelegramForbiddenError",
                                )
                                .values(status="pending", available_at=utcnow(), last_error=None)
                            )
                except Exception as exc:
                    logger.warning("Inbox handler failed: %s", type(exc).__name__)
                    await finish_inbox(sessions, item, exc)
                else:
                    await finish_inbox(sessions, item)
            outgoing = await claim_outbox(sessions)
            if outgoing:
                await process_outbox(sessions, bot, outgoing, settings)
            now = asyncio.get_running_loop().time()
            if now - last_backup_check > 60:
                last_backup_check = now
                try:
                    await prepare_backup(sessions, settings)
                except Exception as exc:
                    logger.warning("Backup preparation failed: %s", type(exc).__name__)
            if not item and not outgoing:
                await asyncio.sleep(0.5)
    finally:
        tasks = [task for task in (calendar_task, polling) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.session.close()
        await engine.dispose()


def run(settings: Settings) -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_worker(settings))
