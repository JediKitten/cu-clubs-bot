"""Idempotent reminders, consented broadcasts and delivery-time audience checks."""

from datetime import UTC, datetime, timedelta
from html import escape
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from cu_clubs import legacy
from cu_clubs.attendance_models import Attendance
from cu_clubs.event_models import Event, EventClub, Registration
from cu_clubs.events import EventService
from cu_clubs.models import Audit, Club, Membership, Outbox, TelegramAccount
from cu_clubs.notification_models import Broadcast
from cu_clubs.services import PermissionDenied, ValidationError

MSK = ZoneInfo("Europe/Moscow")
KINDS = ("notification.reminder", "notification.weekly", "notification.broadcast")


class NotificationsService:
    def __init__(self, session, *, now=None):
        self.session = session
        self.now = now or datetime.now(UTC)
        if self.now.tzinfo is None:
            raise ValueError("An aware timestamp is required")
        self.events = EventService(session)

    async def _enqueue(self, kind, actor, payload, key):
        result = await self.session.execute(
            insert(Outbox)
            .values(
                kind=kind,
                recipient_telegram_id=actor,
                payload=payload,
                dedupe_key=key,
                available_at=self.now,
            )
            .on_conflict_do_nothing(index_elements=[Outbox.dedupe_key])
            .returning(Outbox.id)
        )
        return result.scalar_one_or_none() is not None

    async def _audience(self, event, audience):
        if audience not in {"registered", "attended"}:
            raise ValidationError("Неизвестная аудитория рассылки.")
        query = select(TelegramAccount.telegram_id)
        if audience == "registered":
            query = query.join(
                Registration, Registration.person_id == TelegramAccount.person_id
            ).where(Registration.event_id == event.id, Registration.status == "active")
        else:
            query = query.join(Attendance, Attendance.person_id == TelegramAccount.person_id).where(
                Attendance.event_id == event.id
            )
        return set(await self.session.scalars(query))

    async def _leaders(self):
        # Clubs have no archived/active flag: existing memberships define current leaders.
        return set(
            await self.session.scalars(
                select(TelegramAccount.telegram_id)
                .join(Membership, Membership.person_id == TelegramAccount.person_id)
                .join(Club, Club.id == Membership.club_id)
                .where(Membership.role == "leader")
            )
        )

    async def schedule(self):
        count = 0
        events = list(
            await self.session.scalars(
                select(Event).where(
                    Event.status == "published",
                    Event.starts_at > self.now,
                    Event.starts_at <= self.now + timedelta(hours=24),
                )
            )
        )
        for event in events:
            remaining = event.starts_at - self.now
            # Bounded windows avoid sending a stale day notice minutes before a meeting.
            hours = (
                24
                if timedelta(hours=23) < remaining <= timedelta(hours=24)
                else (1 if timedelta(minutes=55) <= remaining <= timedelta(hours=1) else None)
            )
            if hours is None:
                continue
            schedule = event.starts_at.isoformat()
            for actor in await self._audience(event, "registered"):
                count += await self._enqueue(
                    "notification.reminder",
                    actor,
                    {"event": event.public_id, "schedule": schedule, "hours": hours},
                    f"notification:reminder:{event.id}:{schedule}:{hours}:{actor}",
                )
        local = self.now.astimezone(MSK)
        if local.weekday() == 4 and local.hour >= 16:
            week = (local.date() + timedelta(days=3)).isoformat()
            for actor in await self._leaders():
                count += await self._enqueue(
                    "notification.weekly",
                    actor,
                    {"week": week},
                    f"notification:weekly:{week}:{actor}",
                )
        return count

    async def prepare(self, actor, public_id, audience, body):
        event = await self.events._event(public_id, lock=True)
        await self.events._manage(actor, event)
        if not isinstance(body, str) or not 1 <= len(body.strip()) <= 500:
            raise ValidationError(legacy.text("invalid_mailing_text"))
        recipients = await self._audience(event, audience)
        row = Broadcast(
            event_id=event.id, actor_telegram_id=actor, audience=audience, body=body.strip()
        )
        self.session.add(row)
        await self.session.flush()
        return {
            "id": row.id.hex,
            "body": row.body,
            "count": len(recipients),
            "event": event.public_id,
            "audience": audience,
        }

    async def _broadcast(self, actor, broadcast_id):
        try:
            identifier = UUID(str(broadcast_id))
        except (ValueError, TypeError) as exc:
            raise ValidationError("Рассылка не найдена.") from exc
        row = await self.session.get(Broadcast, identifier, with_for_update=True)
        if row is None or row.actor_telegram_id != actor:
            raise PermissionDenied("Рассылка недоступна.")
        model = await self.session.get(Event, row.event_id)
        event = await self.events._event(model.public_id, lock=True)
        await self.events._manage(actor, event)
        return row, event

    async def confirm(self, actor, broadcast_id):
        row, event = await self._broadcast(actor, broadcast_id)
        if row.confirmed_at is not None:
            return {"queued": 0, "already_confirmed": True}
        recipients = await self._audience(event, row.audience)
        row.confirmed_at = self.now
        for recipient in recipients:
            await self._enqueue(
                "notification.broadcast",
                recipient,
                {"event": event.public_id, "broadcast": row.id.hex},
                f"notification:broadcast:{row.id}:{recipient}",
            )
        self.session.add(
            Audit(
                actor_telegram_id=actor,
                action="notification.broadcast.confirmed",
                entity_id=event.id,
                details={
                    "broadcast": row.id.hex,
                    "recipients": len(recipients),
                    "audience": row.audience,
                },
            )
        )
        return {"queued": len(recipients), "already_confirmed": False}

    async def delivery(self, item):
        """Caller holds transaction through sending, serializing against event changes."""
        kind, actor, payload = item["kind"], item["recipient"], item["payload"]
        if kind == "notification.weekly":
            if actor not in await self._leaders():
                return None
            monday = datetime.fromisoformat(payload["week"]).replace(tzinfo=MSK)
            if not monday - timedelta(days=3) <= self.now.astimezone(MSK) < monday:
                return None
            events = list(
                await self.session.scalars(
                    select(Event)
                    .join(EventClub)
                    .join(Membership, Membership.club_id == EventClub.club_id)
                    .join(TelegramAccount, TelegramAccount.person_id == Membership.person_id)
                    .where(
                        Membership.role == "leader",
                        TelegramAccount.telegram_id == actor,
                        Event.status.in_(["draft", "pending_partner", "published"]),
                        Event.starts_at >= monday,
                        Event.starts_at < monday + timedelta(days=7),
                    )
                    .distinct()
                    .order_by(Event.starts_at)
                )
            )
            # Preserve original copy while excluding the old bot's operational calendar link.
            text = legacy.text("club_owner_weekly_reminder").split("\n\n<a href=")[0]
            text += "\n\nВстречи следующей недели:\n" + (
                "\n".join(
                    f"{event.starts_at.astimezone(MSK):%d.%m %H:%M} · {escape(event.title)}"
                    for event in events[:30]
                )
                or "Пока нет встреч. Добавь их в календарь клуба."
            )
            return {"text": text, "callback": "manage", "label": "Мои клубы"}
        event = await self.events._event(payload["event"], lock=True)
        if kind == "notification.reminder":
            remaining = event.starts_at - self.now if event.starts_at else timedelta(0)
            hours = payload["hours"]
            if (
                event.status != "published"
                or event.starts_at.isoformat() != payload["schedule"]
                or not (
                    timedelta(hours=23) < remaining <= timedelta(hours=24)
                    if hours == 24
                    else timedelta(minutes=55) <= remaining <= timedelta(hours=1)
                )
                or actor not in await self._audience(event, "registered")
            ):
                return None
            text = legacy._render(
                "event_notification_day" if hours == 24 else "event_notification_hour",
                {
                    "{{html .Name}}": escape(event.title),
                    "{{html .Location}}": escape(
                        " · ".join(filter(None, (event.room, event.venue_details))) or "Не указана"
                    ),
                    '{{.StartTime.Format "02.01.2006 15:04"}}': event.starts_at.astimezone(
                        MSK
                    ).strftime("%d.%m.%Y %H:%M"),
                },
            )
        elif kind == "notification.broadcast":
            row = await self.session.get(Broadcast, UUID(payload["broadcast"]))
            if (
                row is None
                or row.event_id != event.id
                or row.confirmed_at is None
                or not await self.events._can_manage(row.actor_telegram_id, event)
                or actor not in await self._audience(event, row.audience)
            ):
                return None
            clubs = await self.events._event_clubs(event)
            text = legacy._render(
                "event_mailing",
                {
                    "{{html .ClubName}}": escape(" · ".join(club.name for club in clubs)),
                    "{{html .EventName}}": escape(event.title),
                    "{{html .Text}}": escape(row.body),
                },
            )
        else:
            raise ValidationError("Неизвестное уведомление.")
        return {
            "text": text,
            "callback": f"event:{event.public_id}:link",
            "label": "Открыть встречу",
        }

    async def queue(self, actor, public_id):
        event = await self.events._event(public_id)
        await self.events._manage(actor, event)
        rows = list(
            await self.session.scalars(
                select(Outbox)
                .where(Outbox.kind.in_(KINDS), Outbox.payload["event"].astext == public_id)
                .order_by(Outbox.created_at.desc())
                .limit(100)
            )
        )
        counts = {
            status: sum(row.status == status for row in rows)
            for status in ("pending", "processing", "sent", "failed", "uncertain")
        }
        return {"counts": counts, "retry": [row.id.hex for row in rows if row.status == "failed"]}

    async def retry(self, actor, public_id):
        event = await self.events._event(public_id, lock=True)
        await self.events._manage(actor, event)
        rows = list(
            await self.session.scalars(
                select(Outbox)
                .where(
                    Outbox.kind.in_(KINDS),
                    Outbox.payload["event"].astext == public_id,
                    Outbox.status == "failed",
                )
                .with_for_update()
            )
        )
        for row in rows:
            row.status, row.last_error, row.available_at = "pending", None, self.now
        self.session.add(
            Audit(
                actor_telegram_id=actor,
                action="notification.retry",
                entity_id=event.id,
                details={"count": len(rows)},
            )
        )
        return len(rows)
