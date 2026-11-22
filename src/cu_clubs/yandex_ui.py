"""Category → name → provider login → explicit Telegram binding confirmation."""

from html import escape
from uuid import uuid4

from aiogram.types import InlineKeyboardButton

from cu_clubs.access import CATEGORIES
from cu_clubs.category_ui import CategoryResult, entry_origin, safe_origin
from cu_clubs.identity import IdentityService, normalize_name
from cu_clubs.services import ValidationError
from cu_clubs.ui_common import Screen, keyboard
from cu_clubs.yandex_auth import YandexAuth, login_url


def screen(text, *rows):
    return Screen(text, keyboard(*rows), banner_role="auth")


def choice_screen(state, notice=""):
    result = screen(
        notice + "Кто ты?\n\nСтуденты и сотрудники входят через университетскую почту в Яндексе.",
        *[
            [(label, f"ya:pick:{state.payload['nonce']}:{key}")]
            for key, label in CATEGORIES.items()
        ],
    )
    if state.payload.get("can_cancel"):
        result.markup.inline_keyboard += keyboard(
            [("Назад", f"ya:cancel:{state.payload['nonce']}")]
        ).inline_keyboard
    return result


def choose(state, origin="profile", *, notice="", can_cancel=False):
    state.state, state.payload = (
        "yandex_choice",
        {
            "resume": safe_origin(origin),
            "nonce": uuid4().hex[:12],
            "can_cancel": can_cancel,
        },
    )
    return choice_screen(state, notice)


def name_screen(state, notice=""):
    name = state.payload.get("full_name")
    return screen(
        (escape(notice) + "\n\n" if notice else "")
        + "Укажи ФИО для личного кабинета."
        + ("\nФИО: " + escape(name) if name else ""),
        [("Назад", f"ya:back:{state.payload['nonce']}")],
    )


def link_screen(state, settings, notice=""):
    key, nonce = state.payload["challenge"], state.payload["nonce"]
    markup = keyboard(
        [("Проверить вход", f"ya:check:{nonce}")],
        [("Получить новую ссылку", f"ya:retry:{nonce}")],
        [("Назад", f"ya:back:{nonce}")],
    )
    markup.inline_keyboard.insert(
        0,
        [
            InlineKeyboardButton(
                text="Войти через Яндекс",
                url=login_url(settings, key),
            )
        ],
    )
    return Screen(
        (escape(notice) + "\n\n" if notice else "")
        + "Войди через Яндекс с почтой @edu.centraluniversity.ru. "
        "После входа вернись сюда и нажми «Проверить вход».\n\nФИО: "
        + escape(state.payload["full_name"]),
        markup,
        banner_role="auth",
    )


async def yandex_gate(session, user_id, state, person, command, data, settings, message=None):
    if not getattr(settings, "yandex_auth_enabled", False):
        return None
    auth = YandexAuth(session, settings)
    active = state.state.startswith("yandex_")
    if data.startswith("ya:") or (active and not command and not data):
        nonce = state.payload.get("nonce")
        if data:
            parts = data.split(":")
            if not active or len(parts) not in {3, 4} or parts[2] != nonce:
                raise ValidationError("Кнопка устарела. Открой вход в личном кабинете.")
            action = parts[1]
        else:
            parts, action = [], "input"
        if action == "cancel" and state.payload.get("can_cancel"):
            if state.payload.get("challenge"):
                row = await auth.owned(user_id, state.payload["challenge"])
                if row.status != "confirmed":
                    row.status = "cancelled"
            origin = safe_origin(state.payload["resume"])
            state.state, state.payload = "idle", {}
            return CategoryResult(resume=origin)
        if action == "back":
            if state.payload.get("challenge"):
                row = await auth.owned(user_id, state.payload["challenge"])
                if row.status != "confirmed":
                    row.status = "cancelled"
            previous = state.state
            state.payload = {
                key: value for key, value in state.payload.items() if key != "challenge"
            }
            state.payload = {**state.payload, "nonce": uuid4().hex[:12]}
            state.state = "yandex_choice" if previous == "yandex_name" else "yandex_name"
            return CategoryResult(
                screen=choice_screen(state)
                if state.state == "yandex_choice"
                else name_screen(state)
            )
        if action == "pick" and state.state == "yandex_choice" and len(parts) == 4:
            if parts[3] not in CATEGORIES:
                raise ValidationError("Неизвестная категория.")
            state.state = "yandex_name"
            state.payload = {**state.payload, "category": parts[3], "nonce": uuid4().hex[:12]}
            return CategoryResult(screen=name_screen(state))
        if state.state == "yandex_name" and message and action == "input":
            try:
                name = normalize_name(message.text or "")
            except ValidationError as error:
                return CategoryResult(screen=name_screen(state, str(error)))
            state.payload = {**state.payload, "full_name": name, "nonce": uuid4().hex[:12]}
            if state.payload["category"] in {"student", "staff"}:
                try:
                    row = await auth.begin(user_id, name, state.payload["category"])
                except ValidationError as error:
                    return CategoryResult(screen=name_screen(state, str(error)))
                state.state = "yandex_link"
                state.payload = {**state.payload, "challenge": str(row.id)}
                return CategoryResult(screen=link_screen(state, settings))
            state.state = "yandex_guest"
        if state.state == "yandex_guest":
            if action == "guest":
                p = await IdentityService(session, "", mail_available=False)._lock_person(user_id)
                if p.verified_at or p.time_login:
                    raise ValidationError("Подтверждённый профиль меняется через администратора.")
                p.full_name, p.kind = state.payload["full_name"], "guest"
                p.declared_category, p.category_selected_at = state.payload["category"], auth.now
                origin = state.payload["resume"]
                state.state, state.payload = "idle", {}
                return CategoryResult(resume=origin)
            return CategoryResult(
                screen=screen(
                    "Категория: "
                    + CATEGORIES[state.payload["category"]]
                    + "\nФИО: "
                    + escape(state.payload["full_name"]),
                    [("Подтвердить", f"ya:guest:{state.payload['nonce']}")],
                    [("Назад", f"ya:back:{state.payload['nonce']}")],
                )
            )
        if action in {"check", "confirm", "retry"} and state.state == "yandex_link":
            if action == "retry":
                try:
                    row = await auth.begin(
                        user_id, state.payload["full_name"], state.payload["category"]
                    )
                except ValidationError as error:
                    return CategoryResult(screen=link_screen(state, settings, str(error)))
                state.payload = {
                    **state.payload,
                    "challenge": str(row.id),
                    "nonce": uuid4().hex[:12],
                }
                return CategoryResult(screen=link_screen(state, settings))
            row = await auth.owned(user_id, state.payload["challenge"])
            if action == "confirm":
                await auth.confirm(user_id, row.id)
                origin = state.payload["resume"]
                state.state, state.payload = "idle", {}
                return CategoryResult(resume=origin)
            if row.status == "candidate" and row.expires_at > auth.now:
                return CategoryResult(
                    screen=screen(
                        "<b>Подтверди привязку почты</b>\n\n"
                        + escape(row.email)
                        + "\nФИО: "
                        + escape(row.full_name)
                        + "\n\nЭто твоя университетская учётная запись?",
                        [("Подтвердить привязку", f"ya:confirm:{nonce}")],
                        [("Войти с другой почтой", f"ya:retry:{nonce}")],
                        [("Назад", f"ya:back:{nonce}")],
                    )
                )
            return CategoryResult(
                screen=link_screen(
                    state,
                    settings,
                    "Вход ещё не завершён."
                    if row.status in {"new", "started", "exchanging"} and row.expires_at > auth.now
                    else "Вход не подтверждён. Получи новую ссылку.",
                )
            )
        if state.state == "yandex_link":
            return CategoryResult(screen=link_screen(state, settings))
        if state.state == "yandex_choice":
            return CategoryResult(screen=choice_screen(state))
        return CategoryResult(screen=name_screen(state))
    explicitly = data.startswith(("auth:", "category:"))
    selected = person.get("verified_at") or (
        person.get("declared_category") in {"applicant", "external"}
        and person.get("category_selected_at")
    )
    if explicitly or not selected:
        route = data.split(":", 1)[1] if explicitly else data
        origin = await entry_origin(session, user_id, command, route, settings)
        return CategoryResult(screen=choose(state, origin, can_cancel=bool(selected)))
    return None
