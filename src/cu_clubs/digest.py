"""Explicit public-only digest snapshots; never consult operational integrations."""

import json
import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import select

from cu_clubs.event_models import Event, EventClub
from cu_clubs.models import Club
from cu_clubs.services import ClubService, PermissionDenied, ValidationError

MOSCOW = ZoneInfo("Europe/Moscow")
MAX_EVENTS = 500
MAX_BYTES = 1_000_000
ANNOUNCEMENTS_URL = "https://time.cu.ru/tsentralnyy-universitet/channels/anonsy-tsu"
WEEKDAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")


def render_post(snapshot):
    """The owner's post format, from the same reviewed snapshot as the cards."""
    days = defaultdict(list)
    for event in sorted(snapshot["events"], key=lambda e: (e["starts_at"], e["id"])):
        start = datetime.fromisoformat(event["starts_at"]).astimezone(MOSCOW)
        days[start.date()].append(event)
    html, markdown, blocks = [], [], []
    for day, events in sorted(days.items()):
        heading = WEEKDAYS[day.weekday()] + ":"
        lines = []
        md_lines = []
        visible = [heading]
        for event in events:
            label = " х ".join(club["name"] for club in event["clubs"])
            url = event["public_url"]
            lines.append(f'➡️ <a href="{escape(url, quote=True)}">{escape(label)}</a>')
            md_label = re.sub(r"([\\\[\]*_`~])", r"\\\1", label)
            md_lines.append(f"➡️ [{md_label}]({url})")
            visible.append("➡️ " + label)
        block = f"<b>{heading}</b>\n\n" + "\n".join(lines)
        html.append(block)
        markdown.append(f"**{heading}**\n\n" + "\n".join(md_lines))
        blocks.append((heading, lines, visible))
    footer_text = "P.S. Подробнее о каждом из мероприятий рассказываем в канале Анонсы ЦУ."
    footer = (
        "<i>P.S. Подробнее о каждом из мероприятий рассказываем в канале "
        f'<a href="{ANNOUNCEMENTS_URL}">Анонсы ЦУ.</a></i>\n\n#<b>Дайджестсобытий</b>'
    )
    html.append(footer)
    markdown.append(
        f"*P.S. Подробнее о каждом из мероприятий рассказываем в канале "
        f"[Анонсы ЦУ.]({ANNOUNCEMENTS_URL})*\n\n#**Дайджестсобытий**"
    )
    # Split only at whole event lines. Each UI page retains rich links and day context.
    pages, current, units = [], [], 0

    def length(value):
        return len(value.encode("utf-16-le")) // 2

    for heading, lines, visible in blocks:
        for index, line in enumerate(lines):
            prefix = f"<b>{heading}</b>\n\n" if index == 0 or not current else ""
            cost = length((heading + "\n\n" if prefix else "") + visible[index + 1]) + 2
            if current and units + cost > 3800:
                pages.append("\n".join(current))
                current, units = [], 0
                prefix = f"<b>{heading}</b>\n\n"
                cost = length(heading + "\n\n" + visible[index + 1]) + 2
            if cost > 3800:
                return "\n\n".join(html), "\n\n".join(markdown), []
            if prefix and current:
                current.append("")
            current.append(prefix + line)
            units += cost
    if current and units + length(footer_text + "\n\n#Дайджестсобытий") + 4 > 3800:
        pages.append("\n".join(current))
        current = []
    if current:
        current.append("")
    current.append(footer)
    pages.append("\n".join(current))
    return "\n\n".join(html), "\n\n".join(markdown), pages


def week_bounds(value):
    try:
        day = date.fromisoformat(value)
    except (ValueError, TypeError):
        raise ValidationError("Введи дату в формате ГГГГ-ММ-ДД.") from None
    day -= timedelta(days=day.weekday())
    return (
        day,
        datetime.combine(day, time(), MOSCOW),
        datetime.combine(day + timedelta(days=7), time(), MOSCOW),
    )


class DigestService:
    def __init__(self, session, settings):
        self.session, self.settings = session, settings

    async def access(self, actor):
        if not await ClubService(self.session).is_admin(actor):
            raise PermissionDenied("Создание дайджеста доступно только администраторам.")

    async def candidates(self, actor, week):
        await self.access(actor)
        _, start, end = week_bounds(week)
        events = list(
            await self.session.scalars(
                select(Event)
                .where(
                    Event.visibility == "open",
                    Event.status.in_(["published", "held"]),
                    Event.starts_at >= start,
                    Event.starts_at < end,
                )
                .order_by(Event.starts_at, Event.public_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        )
        return events

    async def snapshot(self, actor, week, selection, *, all_events=False):
        events = await self.candidates(actor, week)
        current = {e.public_id: e for e in events}
        fresh = (
            {key: event.version for key, event in current.items()}
            if all_events
            else {key: current[key].version for key in selection if key in current}
        )
        if fresh != selection:
            return None, fresh
        if len(selection) > MAX_EVENTS:
            raise ValidationError("В подборке больше 500 встреч. Выбери меньше встреч вручную.")
        username = self.settings.telegram_bot_username or ""
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            raise ValidationError("Для экспорта нужно настроить публичное имя нового бота.")
        output = []
        for event in events:
            if event.public_id not in selection:
                continue
            if not event.ends_at or event.ends_at <= event.starts_at:
                raise ValidationError(
                    "У встречи нет корректного времени окончания. Исправь её перед экспортом."
                )
            clubs = list(
                await self.session.scalars(
                    select(Club)
                    .join(EventClub, EventClub.club_id == Club.id)
                    .where(EventClub.event_id == event.id)
                    .order_by(Club.name, Club.public_id)
                )
            )
            if not clubs:
                raise ValidationError("У встречи нет клуба. Исправь её перед экспортом.")
            room = event.room or ""
            if event.venue_type == "online":
                room = "Онлайн" + (": " + event.venue_details if event.venue_details else "")
            elif event.venue_type == "external":
                room = event.venue_details or room or "Вне кампуса"
            output.append(
                {
                    "id": event.public_id,
                    "version": event.version,
                    "visibility": "open",
                    "status": event.status,
                    "starts_at": event.starts_at.astimezone(MOSCOW).isoformat(),
                    "ends_at": event.ends_at.astimezone(MOSCOW).isoformat(),
                    "clubs": [{"id": club.public_id, "name": club.name} for club in clubs],
                    "title": event.title,
                    "description": event.description,
                    "room": room,
                    "public_url": f"https://t.me/{username}?start=e_{event.public_id}",
                }
            )
        snapshot = {
            "schema_version": 1,
            "timezone": "Europe/Moscow",
            "week_start": week_bounds(week)[0].isoformat(),
            "events": output,
        }
        encoded = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")
        if len(encoded) > MAX_BYTES:
            raise ValidationError("Файл больше 1 МБ. Выбери меньше встреч и повтори экспорт.")
        return encoded, fresh
