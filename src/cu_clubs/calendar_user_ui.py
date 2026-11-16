"""Student week filters, personal history and authorized calendar attachment."""

from datetime import datetime, timedelta
from html import escape

from aiogram.types import BufferedInputFile

from cu_clubs import legacy
from cu_clubs.services import ValidationError
from cu_clubs.ui_common import Screen, keyboard
from cu_clubs.user_calendar import MOSCOW, UserCalendarService

PAGE_SIZE = 8


def _screen(text, rows):
    return Screen(text, keyboard(*rows), banner_role="events")


def _page(items, value):
    try:
        page = int(value)
    except (ValueError, TypeError):
        raise ValidationError("Страница недоступна.") from None
    if not 0 <= page <= max(0, (len(items) - 1) // PAGE_SIZE):
        raise ValidationError("Страница недоступна. Открой список заново.")
    return items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE], page


def _pages(items, page, route):
    row = []
    if page:
        row.append(("←", f"{route}:{page - 1}"))
    if (page + 1) * PAGE_SIZE < len(items):
        row.append(("→", f"{route}:{page + 1}"))
    return [row] if row else []


def _label(event):
    date = datetime.fromisoformat(event["starts_at"]).astimezone(MOSCOW)
    return f"{date:%d.%m %H:%M} · {event['title']}"


async def handle_user_calendar(session, user_id, state, data):
    data = {"future:events": "events", "future:mine": "my_events"}.get(data, data)
    if data == "events":
        data = "uc:week:0:all:0"
    elif data == "my_events":
        data = "uc:mine:upcoming:0"
    elif data.startswith("eventsweek:"):
        data = f"uc:week:{data.split(':', 1)[1]}:all:0"
    if not data.startswith(("uc:", "evics:")):
        return None
    service = UserCalendarService(session)
    if data.startswith("evics:"):
        parts = data.split(":")
        if len(parts) not in {2, 3} or (len(parts) == 3 and parts[2] != "link"):
            raise ValidationError("Кнопка устарела. Открой карточку встречи.")
        content = await service.export(user_id, parts[1], via_link=len(parts) == 3)
        filename = f"event-{parts[1]}.ics"
        screen = _screen(
            legacy._render("event_exported_text", {"{{.FileName}}": escape(filename)}),
            [[(legacy.text("back"), f"event:{parts[1]}" + (":link" if len(parts) == 3 else ""))]],
        )
        screen.document = BufferedInputFile(content, filename=filename)
        return screen
    parts = data.split(":")
    if len(parts) == 4 and parts[1] == "mine":
        section = parts[2]
        events = await service.mine(user_id, section)
        page_events, page = _page(events, parts[3])
        route = f"uc:mine:{section}"
        rows = [[(_label(event), f"event:{event['public_id']}")] for event in page_events]
        rows += _pages(events, page, route)
        rows += [
            [("Предстоящие", "uc:mine:upcoming:0"), ("История", "uc:mine:history:0")],
            [(legacy.text("back"), "menu")],
        ]
        text = (
            legacy.text("my_events_list")
            + "\n\n"
            + ("Предстоящие" if section == "upcoming" else "История")
        )
        if not events:
            text += "\n\n" + (
                "У тебя пока нет предстоящих встреч."
                if section == "upcoming"
                else "История встреч пока пуста."
            )
    elif len(parts) == 5 and parts[1] in {"week", "clubs"}:
        try:
            offset = int(parts[2])
        except ValueError:
            raise ValidationError("Неделя вне доступного диапазона.") from None
        club_id = None if parts[3] == "all" else parts[3]
        week = await service.week(offset, club_id)
        route = f"uc:{parts[1]}:{offset}:{parts[3]}"
        if parts[1] == "clubs":
            clubs, page = _page(week["clubs"], parts[4])
            rows = [[("Все клубы", f"uc:week:{offset}:all:0")]]
            rows += [[(club["name"], f"uc:week:{offset}:{club['public_id']}:0")] for club in clubs]
            rows += _pages(week["clubs"], page, route)
            rows.append([(legacy.text("back"), f"uc:week:{offset}:{parts[3]}:0")])
            end = week["end"] - timedelta(days=1)
            text = f"По клубам\n{week['start']:%d.%m}–{end:%d.%m.%Y}"
            selected = week["selected_club"]
            text += "\nКлуб: " + (escape(selected["name"]) if selected else "все клубы")
            if not week["clubs"]:
                text += "\n\nНа этой неделе открытых встреч пока нет."
        else:
            events, page = _page(week["events"], parts[4])
            rows = [[(_label(event), f"event:{event['public_id']}")] for event in events]
            rows += _pages(week["events"], page, route)
            navigation = []
            if offset > -260:
                navigation.append(("← Неделя", f"uc:week:{offset - 1}:{parts[3]}:0"))
            navigation.append(("Эта неделя", f"uc:week:0:{parts[3]}:0"))
            if offset < 260:
                navigation.append(("Неделя →", f"uc:week:{offset + 1}:{parts[3]}:0"))
            rows += [navigation, [("По клубам", f"uc:clubs:{offset}:{parts[3]}:0")]]
            if club_id:
                rows.append([("Все клубы", f"uc:week:{offset}:all:0")])
            rows.append([(legacy.text("back"), "menu")])
            end = week["end"] - timedelta(days=1)
            text = legacy.text("events_list") + f"\n{week['start']:%d.%m}–{end:%d.%m.%Y}"
            if club_id:
                selected = week["selected_club"]
                text += "\nКлуб: " + (escape(selected["name"]) if selected else "нет встреч")
            if not week["events"]:
                text += "\n\nНа этой неделе открытых встреч пока нет."
    else:
        raise ValidationError("Кнопка устарела. Открой календарь заново.")
    state.state, state.payload = "idle", {"calendar_origin": data}
    return _screen(text, rows)
