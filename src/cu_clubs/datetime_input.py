"""Explicit Moscow dates and clocks accepted by the conversational editor."""

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from cu_clubs.services import ValidationError

MOSCOW = ZoneInfo("Europe/Moscow")
MONTHS = {
    "янв": 1,
    "фев": 2,
    "мар": 3,
    "апр": 4,
    "мая": 5,
    "май": 5,
    "июн": 6,
    "июл": 7,
    "авг": 8,
    "сен": 9,
    "окт": 10,
    "ноя": 11,
    "дек": 12,
}


def parse_date(raw: str, *, anchor: date, today: date | None = None) -> date:
    raw = raw.strip().lower().removesuffix(" г.").removesuffix(" года")
    today = today or datetime.now(MOSCOW).date()
    try:
        if raw in {"сегодня", "завтра", "послезавтра"}:
            return today + timedelta(days={"сегодня": 0, "завтра": 1, "послезавтра": 2}[raw])
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", raw):
            year, month, day = map(int, raw.split("-"))
            return date(year, month, day)
        numeric = re.fullmatch(r"(\d{1,2})([./-])(\d{1,2})(?:\2(\d{2}|\d{4}))?", raw)
        if numeric:
            day, _, month, year = numeric.groups()
            year = anchor.year if year is None else int(year) + (2000 if len(year) == 2 else 0)
            return date(year, int(month), int(day))
        words = re.fullmatch(r"(\d{1,2})\s+([а-яё]+)\.?\s*(\d{4})?", raw)
        if words:
            day, month, year = words.groups()
            number = MONTHS.get(month[:3])
            if number:
                return date(int(year) if year else anchor.year, number, int(day))
    except (ValueError, OverflowError):
        pass
    raise ValidationError(
        "Не распознал дату. Например: 14.11.2026, 14/11, 2026-11-14, 14 ноября или завтра."
    )


def parse_clock(raw: str) -> time:
    raw = raw.strip().lower()
    match = re.fullmatch(r"(\d{1,2})(?:[:.,\s](\d{2}))?", raw)
    try:
        if match:
            hour, minute = match.groups()
            return time(int(hour), int(minute or 0))
        if re.fullmatch(r"\d{3,4}", raw):
            return time(int(raw[:-2]), int(raw[-2:]))
    except ValueError:
        pass
    raise ValidationError(
        "Не распознал время. Например: 19:30, 19.30, 1930 или 19. Время московское."
    )


def parse_combined(raw: str, *, anchor: date) -> datetime:
    parts = re.fullmatch(r"(.+?)(?:\s+(?:в\s+)?|T)(\d{1,2}[:.,]\d{2}|\d{3,4})", raw.strip())
    if not parts:
        raise ValidationError("Укажи дату, затем время следующим шагом.")
    day = parse_date(parts[1], anchor=anchor)
    return datetime.combine(day, parse_clock(parts[2]), MOSCOW)
