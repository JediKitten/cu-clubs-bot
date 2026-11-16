"""Public club-card additions; Telegram routing stays in the adapter."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cu_clubs.event_models import Event, EventClub
from cu_clubs.models import Club

MOSCOW = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True)
class ClubEditPresentation:
    """Reuse the existing exact copy, parser and card without adapter imports."""

    fields: dict
    directions: dict
    parse_value: Callable
    hint: Callable
    card_text: Callable


async def club_card_extras(
    session: AsyncSession,
    club: dict,
    *,
    bot_username: str | None,
    now: datetime | None = None,
    include_direction: bool = True,
) -> dict:
    """Return append-only public-card content and routing metadata.

    The caller appends ``text`` to the existing legacy-compatible card body.
    The returned event links open each event card; ``back_route`` lets a caller
    restore the originating public club card after viewing an event.
    """
    row = await session.scalar(select(Club).where(Club.public_id == club["public_id"]))
    if row is None:
        return {"text": "", "events": [], "back_route": None}
    club_id = row.id
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        raise ValueError("now must include a timezone")
    events = list(
        await session.scalars(
            select(Event)
            .join(EventClub, EventClub.event_id == Event.id)
            .where(
                EventClub.club_id == club_id,
                Event.status == "published",
                Event.visibility == "open",
                Event.starts_at.is_not(None),
                Event.starts_at >= moment,
            )
            .order_by(Event.starts_at, Event.id)
            .limit(3)
        )
    )
    username = (bot_username or "").strip().lstrip("@")
    usable_bot = bool(username) and all(c.isascii() and (c.isalnum() or c == "_") for c in username)
    items = []
    for event in events:
        start = event.starts_at
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        title = escape(event.title or "Встреча")
        url = f"https://t.me/{username}?start=e_{event.public_id}" if usable_bot else None
        items.append(
            {
                "public_id": event.public_id,
                "title": event.title or "Встреча",
                "starts_at": start,
                "url": url,
            }
        )
    direction = club.get("direction")
    lines = []
    if direction and include_direction:
        lines.append("<b>Направление:</b> " + escape(str(direction)))
    if items:
        lines.append("<b>Ближайшие открытые встречи:</b>")
        for item, event in zip(items, events, strict=True):
            if item["url"]:
                title = f'<a href="{escape(item["url"], quote=True)}">{escape(item["title"])}</a>'
            else:
                title = escape(item["title"])
            start = event.starts_at
            if start.tzinfo is None:
                start = start.replace(tzinfo=UTC)
            formatted = escape(start.astimezone(MOSCOW).strftime("%d.%m.%Y, %H:%M"))
            lines.append(f"• {title} · {formatted}")
    return {
        "text": "\n".join(lines),
        "events": items,
        "back_route": {"kind": "club_card", "public_id": club["public_id"]},
    }
