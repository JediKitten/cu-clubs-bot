"""Context-scoped FAQ screens with a handbook link on every screen."""

from html import escape

from aiogram.types import InlineKeyboardButton

from cu_clubs import legacy
from cu_clubs.access import self_registration_enabled
from cu_clubs.faq import HANDBOOK_URL, bot_entries, content, handbook_entries
from cu_clubs.services import ClubService, ValidationError
from cu_clubs.ui_common import Screen, keyboard

SECTIONS = {"clubs": "Про клубы", "levels": "Уровни клубов", "bot": "Как пользоваться ботом"}
ACTIONS = {
    "clubs": (legacy.text("cu_clubs"), "clubs"),
    "events": (legacy.text("events"), "events"),
    "my_events": (legacy.text("my_events"), "my_events"),
    "aqpersonal": ("Мой QR", "aqpersonal"),
}


def _screen(text, rows, *, links=()):
    markup = keyboard(*rows)
    for link in links:
        markup.inline_keyboard.append([InlineKeyboardButton(text=link["label"], url=link["url"])])
    markup.inline_keyboard.append([InlineKeyboardButton(text="Открыть хендбук", url=HANDBOOK_URL)])
    text += f'\n\n<a href="{HANDBOOK_URL}">Хендбук ЦУ · Студенческие клубы</a>'
    return Screen(text, markup, banner_role="menu")


def _entries(section, session):
    return (
        bot_entries(self_registration=self_registration_enabled(session))
        if section == "bot"
        else handbook_entries(section)
    )


def _back(state, section=None):
    if section is not None:
        return f"faq:section:{section}"
    if state.payload.get("faq_scope") == "all":
        return "faq"
    return state.payload.get("faq_back", "menu")


async def handle_faq(session, user_id, state, data):
    if data != "faq" and not data.startswith("faq:"):
        return None
    if data == "faq":
        state.state, state.payload = "idle", {"faq_scope": "all", "faq_back": "menu"}
        return _screen(
            "<b>FAQ</b>\nВыбери раздел справки.",
            [[(label, f"faq:section:{section}")] for section, label in SECTIONS.items()]
            + [[(legacy.text("back"), "menu")]],
        )
    parts = data.split(":")
    if data == "faq:levels":
        # Old club-card callbacks keep their exact origin after the new release.
        club = state.payload.get("club_origin")
        data = f"faq:context:levels:{club}" if club else "faq:section:levels"
        parts = data.split(":")
    if len(parts) == 4 and parts[1] == "context":
        section, origin = parts[2:]
        if section not in SECTIONS:
            raise ValidationError("Раздел справки недоступен.")
        if section == "levels":
            club = await ClubService(session).get_club(origin)
            if club is None:
                raise ValidationError("Клуб не найден.")
            state.state, state.payload = (
                "idle",
                {
                    "faq_scope": "levels",
                    "faq_back": f"club:{origin}",
                    "faq_club": origin,
                },
            )
            entry = next(
                (entry for entry in handbook_entries("levels") if entry["name"] == club["level"]),
                None,
            )
            text = f"<b>{escape(club['level'])}</b>"
            text += "\n\n" + (
                escape(entry["answer"])
                if entry
                else "Правила и возможности уровня смотри в хендбуке."
            )
            text += "\n\nОбновлено: " + escape(content()["handbook"]["verified_at"] or "—")
            return _screen(text, [[(legacy.text("back"), f"club:{origin}")]])
        if origin not in {"profile", "events", "my_events", "clubs", "menu"}:
            raise ValidationError("Переход справки недоступен.")
        state.state, state.payload = "idle", {"faq_scope": section, "faq_back": origin}
        parts = ["faq", "section", section]
    if len(parts) == 3 and parts[1] == "section":
        section = parts[2]
        if state.payload.get("faq_club"):
            raise ValidationError("Открой весь справочник через FAQ в главном меню.")
        if section not in SECTIONS:
            raise ValidationError("Раздел справки недоступен.")
        scope = state.payload.get("faq_scope", "all")
        if scope not in {"all", section}:
            raise ValidationError("Открой нужный раздел через FAQ в главном меню.")
        entries = _entries(section, session)
        rows = [
            [(item.get("question", item.get("name", "Подробнее")), f"faq:entry:{section}:{index}")]
            for index, item in enumerate(entries)
        ]
        rows.append([(legacy.text("back"), _back(state))])
        text = f"<b>{SECTIONS[section]}</b>"
        if not entries:
            text += "\n\nПравила этого раздела смотри в хендбуке."
        return _screen(text, rows)
    if len(parts) == 4 and parts[1] == "entry":
        section = parts[2]
        if state.payload.get("faq_club"):
            raise ValidationError("Открой весь справочник через FAQ в главном меню.")
        if section not in SECTIONS or state.payload.get("faq_scope", "all") not in {"all", section}:
            raise ValidationError("Раздел справки недоступен.")
        entries = _entries(section, session)
        try:
            index = int(parts[3])
            if index < 0:
                raise ValueError
            entry = entries[index]
        except (IndexError, ValueError):
            raise ValidationError("Статья недоступна. Открой справку заново.") from None
        title = entry.get("question", entry.get("name", SECTIONS[section]))
        text = f"<b>{escape(title)}</b>\n\n{escape(entry['answer'])}"
        metadata = content()["bot" if section == "bot" else "handbook"]
        text += "\n\nОбновлено: " + escape(metadata["verified_at"])
        rows = []
        if entry.get("action") in ACTIONS:
            rows.append([ACTIONS[entry["action"]]])
        rows.append([(legacy.text("back"), _back(state, section))])
        return _screen(text, rows, links=entry.get("external_links", []))
    raise ValidationError("Раздел справки недоступен.")
