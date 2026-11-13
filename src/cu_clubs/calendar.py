"""Reconcile published meetings with the owner's existing yearly calendar grid."""

import hashlib
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select, text

from cu_clubs.calendar_models import CalendarBinding
from cu_clubs.event_models import Event, EventClub
from cu_clubs.events import EventService
from cu_clubs.integrations.sheets import (
    CellPatch,
    SheetConflict,
    SheetsClient,
    SheetsError,
    cell_text,
    digest_color,
)
from cu_clubs.models import Audit, Membership, Outbox, Person, TelegramAccount
from cu_clubs.services import ClubService, PermissionDenied, ValidationError

MOSCOW = ZoneInfo("Europe/Moscow")
ROOM_PREFIX = "Аудитория: "
UNKNOWN_ROOM = "Аудиторию уточняем"
MARKER = re.compile(r"\[cu-clubs:event=([a-f0-9]{32})\]")


def now():
    return datetime.now(UTC)


def marker(public_id: str) -> str:
    return f"[cu-clubs:event={public_id}]"


def marked_note(note: str, public_id: str) -> str:
    mark = marker(public_id)
    if mark in note:
        return note
    return note + ("\n" if note else "") + mark


def unmark_note(note: str, public_id: str) -> str:
    mark = marker(public_id)
    if note == mark:
        return ""
    if note.endswith("\n" + mark):
        return note[: -(len(mark) + 1)]
    return note.replace(mark, "")


def parse_room(value: str) -> str | None:
    rows = [line[len(ROOM_PREFIX) :] for line in value.splitlines() if line.startswith(ROOM_PREFIX)]
    if len(rows) != 1:
        raise SheetConflict("calendar_room_unrecognized")
    result = rows[0].strip()
    if len(result) > 300:
        raise SheetConflict("calendar_room_unrecognized")
    return None if result in {"", UNKNOWN_ROOM} else result


def without_room(value: str) -> str:
    return "\n".join(line for line in value.splitlines() if not line.startswith(ROOM_PREFIX))


async def render_event(session, event: Event, bot_username: str, *, room=None) -> str:
    clubs = await EventService(session)._event_clubs(event)
    contacts = list(
        await session.scalars(
            select(Person.time_login)
            .join(Membership, Membership.person_id == Person.id)
            .where(
                Membership.club_id.in_([club.id for club in clubs]),
                Membership.role == "leader",
                Person.verified_at.is_not(None),
            )
            .order_by(Person.time_login)
        )
    )
    contact = (
        ", ".join(sorted({f"{login}@edu.centraluniversity.ru" for login in contacts if login}))
        or "Не указана"
    )
    start, end = event.starts_at.astimezone(MOSCOW), event.ends_at.astimezone(MOSCOW)
    time_text = f"{start:%H:%M}–{end:%H:%M}"
    if start.date() != end.date():
        time_text += f" ({end:%d.%m})"
    lines = [
        "Клуб: " + ", ".join(club.name for club in clubs),
        "Встреча: " + event.title,
        "Время: " + time_text,
        ROOM_PREFIX + (room or UNKNOWN_ROOM),
        "Толк: " + ("да" if event.needs_talk else "нет"),
        "Микрофоны: " + ("да" if event.needs_microphones else "нет"),
        "Почта: " + contact,
        "Ссылка: " + f"https://t.me/{bot_username}?start=e_{event.public_id}",
    ]
    if event.visibility == "closed":
        lines.append("Закрытая · по приглашению")
    if event.venue_details:
        prefix = (
            "Пожелания к аудитории: "
            if event.venue_type == "campus"
            else "Площадка / подключение: "
        )
        lines.append(prefix + event.venue_details)
    if event.status == "cancelled":
        lines.append("Встреча отменена")
    return "\n".join(lines)


def _dto(binding: CalendarBinding, event: Event) -> dict:
    return {
        "id": str(binding.id),
        "event": event.public_id,
        "title": event.title,
        "room": event.room,
        "event_version": event.version,
        "status": binding.status,
        "error": binding.error_code,
        "revision": binding.revision,
        "sheet_title": binding.sheet_title,
        "row": binding.row_index + 1 if binding.row_index is not None else None,
        "column": chr(65 + binding.column_index) if binding.column_index is not None else None,
        "conflict": {key: binding.conflict.get(key) for key in ("kind", "local_room", "sheet_room")}
        if binding.conflict
        else None,
    }


class CalendarService:
    def __init__(self, session):
        self.session = session

    async def _admin(self, actor):
        if not await ClubService(self.session).is_admin(actor):
            raise PermissionDenied("Календарь доступен только администрации.")

    async def status(self, actor):
        await self._admin(actor)
        rows = await self.session.execute(
            select(CalendarBinding, Event).join(Event).order_by(CalendarBinding.updated_at.desc())
        )
        return [_dto(binding, event) for binding, event in rows]

    async def conflicts(self, actor):
        return [
            item for item in await self.status(actor) if item["status"] in {"conflict", "error"}
        ]

    async def resolve(self, actor, binding_id, choice, expected_revision):
        await self._admin(actor)
        try:
            binding = await self.session.get(
                CalendarBinding, UUID(binding_id), with_for_update=True
            )
        except (ValueError, TypeError):
            binding = None
        if binding is None or binding.revision != expected_revision:
            raise ValidationError("Состояние календаря изменилось. Открой актуальную карточку.")
        if (
            choice not in {"local", "sheet"}
            or not binding.conflict
            or binding.conflict.get("kind") != "room"
        ):
            raise ValidationError("Для этого конфликта нужна проверка ячейки в таблице.")
        binding.resolution = {**binding.conflict, "choice": choice, "actor": actor}
        binding.status, binding.error_code, binding.next_sync_at = "pending", None, now()
        binding.revision += 1
        self.session.add(
            Audit(
                actor_telegram_id=actor,
                action="calendar.conflict.resolution_requested",
                entity_id=binding.event_id,
                details={"choice": choice, "revision": binding.revision},
            )
        )
        return _dto(binding, await self.session.get(Event, binding.event_id))

    async def retry(self, actor, binding_id, expected_revision):
        await self._admin(actor)
        try:
            binding = await self.session.get(
                CalendarBinding, UUID(binding_id), with_for_update=True
            )
        except (ValueError, TypeError):
            binding = None
        if binding is None or binding.revision != expected_revision:
            raise ValidationError("Состояние календаря изменилось. Открой актуальную карточку.")
        binding.next_sync_at = now()
        binding.revision += 1
        return _dto(binding, await self.session.get(Event, binding.event_id))

    async def assign_room(self, actor, event_public_id, room, expected_event_version):
        await self._admin(actor)
        return await EventService(self.session).update_event(
            actor, event_public_id, {"room": room}, expected_event_version
        )


class CalendarSync:
    def __init__(self, sessions, settings, *, client=None):
        self.sessions, self.settings = sessions, settings
        self.client = client or SheetsClient(settings)

    async def tick(self, limit=10):
        result = {"checked": 0, "synced": 0, "conflicts": 0, "errors": 0}
        book = self.settings.google_spreadsheet_id
        if not getattr(self.settings, "calendar_sync_enabled", False) or not book:
            return result
        async with self.sessions() as session:
            ids = list(
                await session.scalars(
                    select(Event.id)
                    .outerjoin(
                        CalendarBinding,
                        and_(
                            CalendarBinding.event_id == Event.id,
                            CalendarBinding.spreadsheet_id == book,
                        ),
                    )
                    .where(
                        or_(
                            Event.status.in_(["published", "held"]),
                            and_(Event.status == "cancelled", CalendarBinding.id.is_not(None)),
                        )
                    )
                    .where(
                        or_(
                            CalendarBinding.id.is_(None),
                            CalendarBinding.next_sync_at <= now(),
                            CalendarBinding.event_version != Event.version,
                        )
                    )
                    .order_by(Event.updated_at, Event.id)
                    .limit(max(1, min(limit, 50)))
                )
            )
        for event_id in ids:
            async with self.sessions.begin() as session:
                locked = await session.scalar(
                    text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": "cu-clubs:calendar:" + book},
                )
                if not locked:
                    continue
                event = await session.get(Event, event_id)
                binding = await session.scalar(
                    select(CalendarBinding)
                    .where(
                        CalendarBinding.event_id == event_id, CalendarBinding.spreadsheet_id == book
                    )
                    .with_for_update()
                )
                if binding is None:
                    binding = CalendarBinding(
                        event_id=event_id, spreadsheet_id=book, digest_weeks=[]
                    )
                    session.add(binding)
                    await session.flush()
                result["checked"] += 1
                try:
                    await self._sync(session, event, binding)
                except Exception as exc:
                    code = str(exc) if isinstance(exc, SheetsError) else type(exc).__name__
                    if isinstance(exc, SheetConflict):
                        self._conflict(binding, {"kind": code})
                    else:
                        binding.status, binding.error_code = "error", code[:100]
                        binding.attempts += 1
                        binding.next_sync_at = now() + timedelta(
                            seconds=min(3600, 30 * 2 ** min(binding.attempts, 7))
                        )
                result[
                    "synced"
                    if binding.status == "synced"
                    else "conflicts"
                    if binding.status == "conflict"
                    else "errors"
                ] += 1
        return result

    @staticmethod
    def _conflict(binding, details):
        if binding.status != "conflict" or binding.conflict != details:
            binding.revision += 1
        binding.status, binding.error_code, binding.conflict = "conflict", details["kind"], details
        binding.resolution, binding.next_sync_at = None, now() + timedelta(seconds=60)

    async def _sync(self, session, event, binding):
        if (
            event.starts_at is None
            or event.ends_at is None
            or not self.settings.telegram_bot_username
        ):
            raise SheetsError("event_calendar_fields_missing")
        desired_day = event.starts_at.astimezone(MOSCOW).date()
        original_version = event.version
        link = f"https://t.me/{self.settings.telegram_bot_username}?start=e_{event.public_id}"
        snapshots = {
            year: await self.client.read_year(year)
            for year in {
                desired_day.year,
                binding.baseline_date.year if binding.baseline_date else desired_day.year,
            }
        }
        target = snapshots[desired_day.year]
        owned = []
        orphan_link = False
        for snapshot in snapshots.values():
            for (row, col), cell in snapshot.cells.items():
                note, value = cell.get("note", ""), cell_text(cell)
                matches = MARKER.findall(note)
                if event.public_id in matches:
                    if matches.count(event.public_id) != 1 or link not in value:
                        raise SheetConflict("calendar_identity_changed")
                    owned.append((snapshot, row, col, cell))
                elif link in value:
                    orphan_link = True
        if len(owned) > 1:
            raise SheetConflict("calendar_identity_ambiguous")
        if not owned and (orphan_link or binding.baseline_text):
            raise SheetConflict("calendar_identity_missing")
        local_room, sheet_room = event.room, event.room
        old = owned[0] if owned else None
        if old:
            snapshot, row, col, cell = old
            actual_day = snapshot.date_at(row, col)
            if (
                actual_day is None
                or snapshot.blocked(row, col)
                or "formulaValue" in cell.get("userEnteredValue", {})
            ):
                raise SheetConflict("calendar_cell_unavailable")
            if binding.baseline_date and actual_day not in {binding.baseline_date, desired_day}:
                raise SheetConflict("calendar_moved_manually")
            sheet_room = parse_room(cell_text(cell))
            base_text = binding.baseline_text or (binding.pending_write or {}).get("text")
            base_room = (
                binding.baseline_room
                if binding.baseline_text is not None
                else (binding.pending_write or {}).get("room", local_room)
            )
            if base_text is None:
                base_text = await render_event(
                    session, event, self.settings.telegram_bot_username, room=local_room
                )
            if without_room(cell_text(cell)) != without_room(base_text):
                raise SheetConflict("calendar_text_changed_manually")
            if local_room != base_room and sheet_room != base_room and local_room != sheet_room:
                details = {
                    "kind": "room",
                    "local_room": local_room,
                    "sheet_room": sheet_room,
                    "event_version": original_version,
                    "sheet_hash": hashlib.sha256(cell_text(cell).encode()).hexdigest(),
                }
                resolution = binding.resolution
                if resolution and all(
                    resolution.get(key) == value for key, value in details.items()
                ):
                    sheet_room = local_room if resolution["choice"] == "local" else sheet_room
                else:
                    self._conflict(binding, details)
                    return
            elif sheet_room == base_room:
                sheet_room = local_room
        desired_text = await render_event(
            session, event, self.settings.telegram_bot_username, room=sheet_room
        )
        moving = old is not None and (
            old[0].year != desired_day.year or old[0].date_at(old[1], old[2]) != desired_day
        )
        if old and not moving:
            row, col = old[1], old[2]
            note = old[3].get("note", "")
        else:
            row, col = target.free_slot(desired_day)
            note = marked_note(target.cell(row, col).get("note", ""), event.public_id)
        patches = []
        if not old or moving or cell_text(old[3]) != desired_text:
            patches.append(CellPatch(target, row, col, desired_text, note))
        if moving:
            patches.append(
                CellPatch(
                    old[0],
                    old[1],
                    old[2],
                    None,
                    unmark_note(old[3].get("note", ""), event.public_id),
                )
            )
        if patches:
            binding.pending_write = {
                "text": desired_text,
                "room": sheet_room,
                "date": desired_day.isoformat(),
            }
            await self.client.guarded_write(patches)
        if sheet_room != local_room:
            if event.status != "published":
                raise SheetConflict("calendar_room_history_requires_review")
            fresh = await session.scalar(
                select(Event)
                .where(Event.id == event.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if fresh.version != original_version:
                raise SheetConflict("event_changed_during_calendar_sync")
            before = fresh.room
            fresh.room, fresh.version = sheet_room, fresh.version + 1
            session.add(
                Audit(
                    actor_telegram_id=(binding.resolution or {}).get("actor"),
                    action="calendar.room.changed",
                    entity_id=fresh.id,
                    details={"before": before, "room": sheet_room},
                )
            )
            await EventService(session)._notify(fresh, "event.changed")
            original_version = fresh.version
        current_snapshot = await self.client.read_year(desired_day.year) if patches else target
        current_cell = current_snapshot.cell(row, col)
        if cell_text(current_cell) != desired_text or marker(
            event.public_id
        ) not in current_cell.get("note", ""):
            raise SheetConflict("calendar_write_verification_failed")
        await self._digest(session, event, binding, desired_day, digest_color(current_cell))
        binding.sheet_id, binding.sheet_title = target.sheet_id, target.title
        binding.row_index, binding.column_index = row, col
        binding.baseline_text, binding.baseline_room, binding.baseline_date = (
            desired_text,
            sheet_room,
            desired_day,
        )
        binding.event_version, binding.status, binding.error_code = original_version, "synced", None
        binding.conflict, binding.resolution, binding.pending_write = None, None, None
        binding.attempts, binding.next_sync_at = 0, now() + timedelta(seconds=60)

    async def _digest(self, session, event, binding, day, highlighted):
        week = (day - timedelta(days=day.weekday())).isoformat()
        notified = list(binding.digest_weeks or [])
        first = (
            binding.baseline_digest is None
            or binding.baseline_date is None
            or binding.baseline_date - timedelta(days=binding.baseline_date.weekday())
            != day - timedelta(days=day.weekday())
        )
        if highlighted and week not in notified:
            notified.append(week)
            if not first:
                recipients = set(
                    await session.scalars(
                        select(TelegramAccount.telegram_id)
                        .join(Membership, Membership.person_id == TelegramAccount.person_id)
                        .join(EventClub, EventClub.club_id == Membership.club_id)
                        .where(EventClub.event_id == event.id, Membership.role == "leader")
                    )
                )
                for recipient in recipients:
                    session.add(
                        Outbox(
                            kind="calendar.digest.notice",
                            recipient_telegram_id=recipient,
                            payload={"event": event.public_id, "title": event.title, "week": week},
                            dedupe_key=f"calendar.digest:{event.id}:{week}:{recipient}",
                        )
                    )
        binding.digest_weeks, binding.baseline_digest = notified, highlighted
