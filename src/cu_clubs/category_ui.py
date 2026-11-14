"""Temporary self-declared categories; resumption contains view routes only."""

import re
from dataclasses import dataclass
from uuid import uuid4

from cu_clubs.access import CATEGORIES, self_registration_enabled
from cu_clubs.identity import IdentityService, normalize_name
from cu_clubs.services import ValidationError
from cu_clubs.ui_common import Screen, keyboard


@dataclass
class CategoryResult:
    screen: Screen | None = None
    resume: str | None = None


def safe_origin(value):
    if value in {"menu", "profile", "aqpersonal", "events", "my_events", "clubs"}:
        return value
    if re.fullmatch(r"(?:club|event):[A-Za-z0-9_-]{1,80}(?::link)?", value or ""):
        return value
    if re.fullmatch(r"(?:apilink|aqi):[0-9a-fA-F-]{32,36}", value or ""):
        return value
    return "menu"


def category_screen(state, notice=""):
    text = "Кто ты?\n\nВыбери категорию. Пока подтверждать почту не нужно."
    if notice:
        text = notice + "\n\n" + text
    rows = [
        [(label, f"categorypick:{state.payload['nonce']}:{category}")]
        for category, label in CATEGORIES.items()
    ]
    if state.payload.get("changing"):
        rows.append([("Назад", f"categorycancel:{state.payload['nonce']}")])
    return Screen(text, keyboard(*rows), banner_role="auth")


def begin_category(state, origin="menu", *, changing=False):
    state.state, state.payload = (
        "category_choice",
        {
            "resume": safe_origin(origin),
            "changing": changing,
            "nonce": uuid4().hex[:12],
        },
    )
    return category_screen(state)


def guest_name_screen(state, notice=""):
    from html import escape

    text = "Укажи ФИО для гостевого профиля. Почта и логин в Тайме не нужны."
    if state.payload.get("full_name"):
        text += "\nФИО: " + escape(state.payload["full_name"])
    return Screen(
        (escape(notice) + "\n\n" if notice else "") + text,
        keyboard([("Назад", f"categoryback:{state.payload['nonce']}")]),
        banner_role="auth",
    )


async def entry_origin(session, user_id, command, data, settings):
    action = data
    if command and command[0].split("@", 1)[0] == "/start":
        if len(command) != 2:
            return "menu"
        payload = command[1]
        if payload.startswith("e_"):
            return safe_origin(f"event:{payload[2:]}:link")
        if payload.startswith("c_"):
            return safe_origin(f"club:{payload[2:]}")
        action = (
            "aq:" + payload[2:]
            if payload.startswith("a_")
            else "apilinkopen:" + payload[2:]
            if payload.startswith("l_")
            else "menu"
        )
    if action.startswith("aq:"):
        from cu_clubs.attendance import AttendanceService

        result = await AttendanceService(session, settings.verification_secret).resolve_qr(
            user_id, action[3:]
        )
        return "aqi:" + result["qr_id"]
    if action.startswith("apilinkopen:"):
        from cu_clubs.api_service import ApiService

        result = await ApiService(session, settings).open_link(user_id, action.split(":", 1)[1])
        return "apilink:" + result["id"]
    if action.startswith("evreg:"):
        parts = action.split(":")
        return safe_origin("event:" + parts[1] + (":link" if parts[-1] == "link" else ""))
    return safe_origin(action)


async def category_gate(session, user_id, state, person, command, data, settings, message=None):
    if not self_registration_enabled(session):
        return None
    selected = bool(person.get("declared_category") and person.get("category_selected_at"))
    if state.state in {"category_name", "category_preview"} and not command:
        nonce = state.payload["nonce"]
        if data == f"categoryback:{nonce}":
            if state.state == "category_preview":
                state.state = "category_name"
                state.payload = {**state.payload, "nonce": uuid4().hex[:12]}
                return CategoryResult(screen=guest_name_screen(state))
            return CategoryResult(
                screen=begin_category(state, state.payload["resume"], changing=selected)
            )
        if state.state == "category_name" and message:
            try:
                name = normalize_name(message.text or "")
            except ValidationError as error:
                return CategoryResult(screen=guest_name_screen(state, str(error)))
            state.state = "category_preview"
            state.payload = {**state.payload, "full_name": name, "nonce": uuid4().hex[:12]}
        elif data == f"categoryconfirm:{nonce}" and state.state == "category_preview":
            await IdentityService(session, "", mail_available=False).declare_category(
                user_id, state.payload["category"], full_name=state.payload["full_name"]
            )
            origin = safe_origin(state.payload["resume"])
            state.state, state.payload = "idle", {}
            return CategoryResult(resume=origin)
        elif data:
            raise ValidationError("Кнопка устарела. Продолжи ввод ФИО или нажми «Назад».")
        if state.state == "category_name":
            return CategoryResult(screen=guest_name_screen(state))
        from html import escape

        return CategoryResult(
            screen=Screen(
                "Категория: "
                + CATEGORIES[state.payload["category"]]
                + "\nФИО: "
                + escape(state.payload["full_name"]),
                keyboard(
                    [("Подтвердить", f"categoryconfirm:{state.payload['nonce']}")],
                    [("Назад", f"categoryback:{state.payload['nonce']}")],
                ),
                banner_role="auth",
            )
        )
    if data.startswith(("categorypick:", "categorycancel:")):
        parts = data.split(":")
        if state.state != "category_choice" or parts[1] != state.payload.get("nonce"):
            raise ValidationError("Кнопка устарела. Открой выбор категории в личном кабинете.")
        if parts[0] == "categorycancel":
            if not selected or not state.payload.get("changing") or len(parts) != 2:
                raise ValidationError("Сначала выбери категорию.")
        else:
            if len(parts) != 3 or parts[2] not in CATEGORIES:
                raise ValidationError("Неизвестная категория.")
            if (
                parts[2] in {"applicant", "external"}
                and person.get("kind") != "guest"
                and not person.get("verified_at")
                and not person.get("time_login")
            ):
                state.state = "category_name"
                state.payload = {
                    **state.payload,
                    "category": parts[2],
                    "nonce": uuid4().hex[:12],
                }
                return CategoryResult(screen=guest_name_screen(state))
            await IdentityService(session, "", mail_available=False).declare_category(
                user_id, parts[2]
            )
        origin = safe_origin(state.payload.get("resume"))
        state.state, state.payload = "idle", {}
        return CategoryResult(resume=origin)
    if data == "category:profile":
        return CategoryResult(screen=begin_category(state, "profile", changing=selected))
    if selected:
        return None
    if state.state == "category_choice" and not command:
        return CategoryResult(screen=category_screen(state))
    origin = await entry_origin(session, user_id, command, data, settings)
    return CategoryResult(screen=begin_category(state, origin))
