"""Shared student-calendar queries and safe RFC 5545 export; no delivery."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from icalendar import Calendar
from icalendar import Event as ICalendarEvent
from sqlalchemy import exists, or_, select

from cu_clubs.attendance_models import Attendance
from cu_clubs.event_models import Event, Registration
from cu_clubs.events import EventService, _date
from cu_clubs.services import ValidationError

MOSCOW = ZoneInfo("Europe/Moscow")


def week_bounds(offset: int, now: datetime | None = None):
    if isinstance(offset, bool) or not isinstance(offset, int) or not -260 <= offset <= 260:
        raise ValidationError("Неделя вне доступного диапазона.")
    now = (_date(now) if now else datetime.now(UTC)).astimezone(MOSCOW)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start += timedelta(days=-now.weekday(), weeks=offset)
    return start, start + timedelta(days=7)


def event_ics(event: dict) -> bytes:
    """Serialize an already-authorized card using a field allowlist.

    No attendee, author, Telegram ID, private link or post-registration text is
    exported. The library escapes and folds text, including Cyrillic and CRLF.
    """
    if not event.get("starts_at") or not event.get("ends_at"):
        raise ValidationError("Сначала укажи начало и окончание встречи.")
    calendar = Calendar()
    calendar.add("prodid", "-//CU Clubs//Student calendar//RU")
    calendar.add("version", "2.0")
    item = ICalendarEvent()
    item.add("uid", f"{event['public_id']}@cu-clubs")
    item.add("sequence", event["version"])
    item.add("dtstamp", _date(event.get("updated_at")) or datetime.now(UTC))
    item.add("dtstart", _date(event["starts_at"]))
    item.add("dtend", _date(event["ends_at"]))
    item.add("summary", event["title"])
    description = [event["description"]]
    if event.get("clubs"):
        description.append("Клубы: " + ", ".join(club["name"] for club in event["clubs"]))
    if event.get("public_contact"):
        description.append("Ответственный: " + event["public_contact"])
    item.add("description", "\n\n".join(description))
    item.add("location", event.get("room") or event.get("venue_details") or "Аудиторию уточняем")
    item.add("class", "PRIVATE" if event["visibility"] == "closed" else "PUBLIC")
    item.add("status", "CANCELLED" if event["status"] == "cancelled" else "CONFIRMED")
    calendar.add_component(item)
    return calendar.to_ical()


class UserCalendarService(EventService):
    async def week(self, offset=0, club_public_id=None, *, now=None):
        start, end = week_bounds(offset, now)
        events = await self.public_events(start, end)
        # Derive the filter from the same public dataset, including partners.
        clubs = {club["public_id"]: club for event in events for club in event["clubs"]}
        selected = clubs.get(club_public_id)
        if club_public_id:
            events = [
                event
                for event in events
                if any(club["public_id"] == club_public_id for club in event["clubs"])
            ]
        return {
            "start": start,
            "end": end,
            "events": events,
            "clubs": sorted(clubs.values(), key=lambda club: (club["name"], club["public_id"])),
            "selected_club": selected,
        }

    async def mine(self, actor_id, section="upcoming", *, now=None):
        if section not in {"upcoming", "history"}:
            raise ValidationError("Неизвестный раздел моих встреч.")
        person = await self._person(actor_id)
        now = _date(now) if now else datetime.now(UTC)
        registered = exists().where(
            Registration.event_id == Event.id,
            Registration.person_id == person.id,
            Registration.status == "active",
        )
        attended = exists().where(
            Attendance.event_id == Event.id, Attendance.person_id == person.id
        )
        query = select(Event).where(
            Event.status.in_(["published", "held", "cancelled"]), or_(registered, attended)
        )
        history = or_(Event.status.in_(["held", "cancelled"]), Event.ends_at <= now)
        query = query.where(~history if section == "upcoming" else history)
        order = Event.starts_at.asc() if section == "upcoming" else Event.starts_at.desc()
        events = await self.session.scalars(query.order_by(order, Event.public_id))
        return [await self._dto(event, actor_id) for event in events]

    async def export(self, actor_id, public_id, *, via_link=False):
        event = await self.get_event(public_id, actor_id, via_link=via_link)
        if event["status"] not in {"published", "held", "cancelled"}:
            raise ValidationError("Экспорт доступен после публикации встречи.")
        return event_ics(event)
