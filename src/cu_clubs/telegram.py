"""Private-chat Telegram adapter; dialogue state lives in PostgreSQL, not aiogram FSM."""

from html import escape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.types import Update as AiogramUpdate
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from cu_clubs import legacy
from cu_clubs.attendance_ui import handle_attendance
from cu_clubs.calendar_ui import handle_calendar
from cu_clubs.event_ui import handle_events
from cu_clubs.identity_ui import handle_identity
from cu_clubs.models import Conversation, TelegramUpdate
from cu_clubs.screens import ScreenManager, UIScreen
from cu_clubs.services import ClubNotFound, ClubService, PermissionDenied, ValidationError
from cu_clubs.telegram_files import attendance_upload
from cu_clubs.template_ui import handle_templates
from cu_clubs.ui_common import keyboard

UNAVAILABLE = "Этот раздел ещё в разработке. Сейчас доступны клубы и управление профилем клуба."
FIELDS = {
    "name": legacy.text("set_club_name"),
    "description": legacy.text("set_club_description"),
    "channel": legacy.text("set_club_link"),
    "cover": legacy.text("set_club_avatar"),
    "public_team": "Публичная команда",
}


def card_text(club: dict[str, Any], *, subscribed: bool = False) -> str:
    team = club.get("public_team") or []
    people = (
        "\n".join(
            f"• {escape(member['name'])}"
            + (f" — {escape(member['role'])}" if member.get("role") else "")
            for member in team
        )
        or "Команда пока не указана"
    )
    base = legacy.club_base_text(club)
    if subscribed:
        heading = f"Клуб: <b>{escape(club['name'])}</b>"
        base = base.replace(heading, heading + "\nПодписка включена", 1)
    return (
        base
        + f"\n\n<b>Уровень:</b> {escape(club['level'])}\n\n<b>Команда</b>\n{people}"
        + "".join(
            f"\n{escape(link['label'])}: {escape(link['url'])}" for link in club.get("links", [])
        )
        + "\n\nБлижайшие встречи — в разделе «Мероприятия»."
    )


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


async def _send_text(bot: Bot, user_id: int, text: str, markup: Any) -> None:
    # Telegram limits message text to 4096 UTF-16 units. Large cards stay readable
    # as plain text rather than splitting HTML tags or dropping profile content.
    if len(text.encode("utf-16-le")) // 2 <= 3900:
        await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=markup)
        return
    parser = _PlainText()
    parser.feed(text)
    chunks: list[str] = []
    chunk, size = "", 0
    for char in "".join(parser.parts):
        units = len(char.encode("utf-16-le")) // 2
        if size + units > 3900:
            chunks.append(chunk)
            chunk, size = "", 0
        chunk += char
        size += units
    if chunk:
        chunks.append(chunk)
    for index, part in enumerate(chunks):
        await bot.send_message(
            user_id,
            part,
            parse_mode=None,
            reply_markup=markup if index == len(chunks) - 1 else None,
        )


async def _send_screen(
    bot: Bot,
    user_id: int,
    text: str,
    markup: Any,
    photo: Any,
    *,
    banner_role: str | None = None,
    file_ids: dict[tuple[int, str], str] | None = None,
) -> None:
    if photo is not None:
        key = (bot.id, banner_role) if banner_role else None
        if key is not None and file_ids is not None:
            photo = file_ids.get(key, photo)
        if len(text.encode("utf-16-le")) // 2 <= 1000:
            result = await bot.send_photo(
                user_id, photo, caption=text, parse_mode="HTML", reply_markup=markup
            )
            if key is not None and file_ids is not None and getattr(result, "photo", None):
                file_ids[key] = result.photo[-1].file_id
            return
        result = await bot.send_photo(user_id, photo)
        if key is not None and file_ids is not None and getattr(result, "photo", None):
            file_ids[key] = result.photo[-1].file_id
    if text:
        await _send_text(bot, user_id, text, markup)


def _callback_banner(data: str) -> str | None:
    if data in {"menu", "cancel", "admin", "faq", "faq:levels"}:
        return "menu"
    if data == "profile":
        return "personal-account"
    if data == "future:events":
        return "events"
    if data == "future:mine":
        return "personal-account"
    if data == "clubs" or data.startswith(("club:", "subscribe:")):
        return "clubs"
    if data == "manage" or data.startswith(
        ("manageclub:", "editmenu:", "edit:", "save:", "cancel:", "organizers:", "org")
    ):
        return "club-owner"
    return None


def _confirmation(action: str, state: Conversation) -> InlineKeyboardMarkup:
    token = state.payload["token"]
    if action == "save":
        return keyboard(
            [(legacy.text("confirm"), f"save:{token}")],
            [
                (
                    "Назад",
                    f"edit:{state.payload['club']}:{state.payload['field']}"
                    + (":seq" if state.payload.get("sequence") else ""),
                )
            ],
            [("Отменить изменение", f"cancel:{token}")],
        )
    return keyboard(
        [(legacy.text("confirm"), f"{action}:{token}"), (legacy.text("cancel"), f"cancel:{token}")]
    )


def _club_edit_hint(field):
    return {
        "public_team": "Пришли список: один человек на строку, «Имя — роль». "
        "Это публичное описание, оно не выдаёт права. Для очистки — «Удалить».",
        "name": legacy.text("input_club_name"),
        "description": legacy.text("input_club_description"),
        "cover": legacy.text("input_club_avatar") + "\n\nДля очистки — «Удалить».",
        "channel": legacy.text("input_club_link")
        + "\n\nМожно указать @username; для очистки — «Удалить».",
    }[field]


def _club_edit_navigation(payload):
    public_id, field = payload["club"], payload["field"]
    index = list(FIELDS).index(field)
    back = (
        f"edit:{public_id}:{list(FIELDS)[index - 1]}:seq"
        if payload.get("sequence") and index
        else f"editmenu:{public_id}"
    )
    rows = [[("Назад", back)]]
    if payload.get("sequence") and index:
        rows.append([("Выбрать другое поле", f"editmenu:{public_id}")])
    return keyboard(*rows)


def parse_value(field: str, message: Message) -> Any:
    text = (message.text or "").strip()
    if field == "cover":
        if message.photo:
            return message.photo[-1].file_id
        if text.lower() == "удалить":
            return None
        raise ValidationError("Пришли фотографию или слово «Удалить».")
    if not text:
        raise ValidationError("Пришли значение текстом.")
    if field == "name":
        if not 2 <= len(text) <= 100:
            raise ValidationError("Название должно содержать от 2 до 100 символов.")
        return text
    if field == "description":
        if len(text) > 3000:
            raise ValidationError("Описание должно содержать не больше 3000 символов.")
        return text
    if field == "channel":
        if text.lower() == "удалить":
            return None
        if text.startswith("@") and len(text) > 1:
            text = "https://t.me/" + text[1:]
        if not text.startswith("https://") or len(text) > 500 or any(c.isspace() for c in text):
            raise ValidationError("Пришли ссылку https://… или @username; для очистки — «Удалить».")
        return text
    if field == "public_team":
        if text.lower() == "удалить":
            return []
        if len(text) > 2000:
            raise ValidationError("Список команды слишком длинный: максимум 2000 символов.")
        people = []
        for line in text.splitlines():
            if not line.strip():
                continue
            name, separator, role = line.partition(" — ")
            if not name.strip() or len(name.strip()) > 200 or len(role.strip()) > 200:
                raise ValidationError("Имя и роль участника: до 200 символов; имя обязательно.")
            people.append({"name": name.strip(), "role": role.strip() if separator else ""})
        if len(people) > 20:
            raise ValidationError("Добавь не больше 20 участников публичной команды.")
        return people
    raise ValidationError("Неизвестное поле карточки.")


async def _conversation(session: Any, user_id: int) -> Conversation:
    await session.execute(
        insert(Conversation)
        .values(
            telegram_id=user_id,
            state="idle",
            payload={},
        )
        .on_conflict_do_nothing(index_elements=[Conversation.telegram_id])
    )
    result = await session.execute(
        select(Conversation)
        .where(
            Conversation.telegram_id == user_id,
        )
        .with_for_update()
    )
    return result.scalar_one()


def _reset(state: Conversation) -> None:
    state.state, state.payload = "idle", {}


async def _profile_access(service: ClubService, user_id: int, public_id: str) -> dict:
    permissions = await service.permissions(user_id, public_id)
    if not (permissions["is_admin"] or permissions["is_leader"] or permissions["edit_profile"]):
        raise PermissionDenied("Нет права редактировать профиль этого клуба.")
    club = await service.get_club(public_id)
    if club is None:
        raise ClubNotFound("Клуб не найден.")
    return club


async def _leader_access(service: ClubService, user_id: int, public_id: str) -> None:
    permissions = await service.permissions(user_id, public_id)
    if not (permissions["is_admin"] or permissions["is_leader"]):
        raise PermissionDenied("Организаторами управляют лидер и администрация.")


async def _menu(service: ClubService, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    rows = [
        [(legacy.text("cu_clubs"), "clubs"), (legacy.text("events"), "events")],
        [(legacy.text("personal_account"), "profile")],
        [(legacy.text("my_events"), "my_events")],
        [("FAQ", "faq")],
    ]
    if await service.managed_clubs(user_id):
        rows.append([(legacy.text("my_clubs"), "manage")])
    if await service.is_admin(user_id):
        rows.append([(legacy.text("admin_menu"), "admin")])
    return legacy.text("main_menu_text"), keyboard(*rows)


async def _clubs(service: ClubService, *, manage: bool, user_id: int) -> tuple[str, Any]:
    clubs = await service.managed_clubs(user_id) if manage else await service.list_clubs()
    prefix = "manageclub" if manage else "club"
    rows = [[(club["name"], f"{prefix}:{club['public_id']}")] for club in clubs]
    rows.append([("В меню", "menu")])
    title = legacy.owned_clubs_text(len(clubs)) if manage else legacy.text("cu_clubs_list")
    if not clubs and not manage:
        title += "\n\nКлубы пока не добавлены."
    return title, keyboard(*rows)


async def _club_card(service: ClubService, user_id: int, public_id: str) -> tuple[str, Any, Any]:
    club = await service.get_club(public_id)
    if club is None:
        raise ClubNotFound("Клуб не найден.")
    subscribed = await service.get_subscription(user_id, public_id)
    return (
        card_text(club, subscribed=subscribed),
        keyboard(
            [
                (
                    legacy.text("disable_mailing_from_this_club")
                    if subscribed
                    else legacy.text("enable_mailing_from_this_club"),
                    f"subscribe:{public_id}:{0 if subscribed else 1}",
                )
            ],
            [("Поделиться", f"share:{public_id}"), ("Что даёт уровень?", "faq:levels")],
            [(legacy.text("cu_clubs"), "clubs"), ("В меню", "menu")],
        ),
        club.get("cover"),
    )


async def _edit_menu(service: ClubService, user_id: int, public_id: str) -> tuple[str, Any]:
    club = await _profile_access(service, user_id, public_id)
    return legacy.text("profile_text") + f"\n\n{escape(club['name'])}", keyboard(
        *[[(name, f"edit:{public_id}:{field}")] for field, name in FIELDS.items()],
        [("Посмотреть карточку", f"club:{public_id}")],
        [(legacy.text("back"), f"manageclub:{public_id}"), ("В меню", "menu")],
    )


async def _organizers(service: ClubService, user_id: int, public_id: str) -> tuple[str, Any]:
    await _leader_access(service, user_id, public_id)
    organizers = await service.list_organizers(user_id, public_id)
    lines = ["<b>Организаторы</b>", "Публичная команда карточки редактируется отдельно."]
    rows = []
    for person in organizers:
        permissions = (
            " / ".join(
                name
                for key, name in [
                    ("edit_profile", "профиль"),
                    ("manage_events", "встречи"),
                    ("record_attendance", "посещения"),
                ]
                if person[key]
            )
            or "без дополнительных прав"
        )
        lines.append(f"\n{escape(person['full_name'])} · {person['telegram_id']}\n{permissions}")
        if person["role"] != "leader":
            rows.append(
                [(f"Права: {person['full_name'][:35]}", f"org:{public_id}:{person['telegram_id']}")]
            )
    rows.extend(
        [[("Добавить организатора", f"orgadd:{public_id}")], [("Назад", f"manageclub:{public_id}")]]
    )
    return "\n".join(lines), keyboard(*rows)


async def _org_detail(
    service: ClubService, user_id: int, public_id: str, target: int
) -> tuple[str, Any]:
    await _leader_access(service, user_id, public_id)
    people = await service.list_organizers(user_id, public_id)
    person = next((item for item in people if item["telegram_id"] == target), None)
    if person is None or person["role"] == "leader":
        raise ValidationError("Этот организатор недоступен для изменения.")
    flags = [person[key] for key in ("edit_profile", "manage_events", "record_attendance")]
    rows = []
    for index, name in enumerate(("Редактировать профиль", "Управлять встречами", "Отмечать")):
        changed = flags.copy()
        changed[index] = not changed[index]
        mask = "".join("1" if value else "0" for value in changed)
        rows.append(
            [(f"{'✓' if flags[index] else '○'} {name}", f"orgset:{public_id}:{target}:{mask}")]
        )
    rows.extend(
        [
            [("Удалить организатора", f"orgremove:{public_id}:{target}")],
            [("Назад", f"organizers:{public_id}")],
        ]
    )
    return f"Права: {escape(person['full_name'])} · {target}", keyboard(*rows)


def build_dispatcher(sessionmaker: Any, settings: Any) -> Dispatcher:
    """Build once at startup. Caller durably stores/leases and deduplicates updates.

    Each handler owns its database transaction and sends Telegram replies after commit.
    Only private chats are handled. This adapter never starts polling or reads secrets.
    """
    dispatcher = Dispatcher(disable_fsm=True)
    banner_file_ids: dict[tuple[int, str], str] = {}
    router = Router(name="private_clubs")
    router.message.filter(F.chat.type == "private")
    router.callback_query.filter(F.message.chat.type == "private")

    async def dispatch(
        event: Message | CallbackQuery,
        bot: Bot,
        event_update: AiogramUpdate,
        inbox_attempt: int | None = None,
    ) -> None:
        user = event.from_user
        if user is None:
            return
        callback = event if isinstance(event, CallbackQuery) else None
        message = event if isinstance(event, Message) else None
        manager = ScreenManager(sessionmaker, bot, file_ids=banner_file_ids)
        if callback and await manager.handle_page(callback):
            await callback.answer()
            return
        command = (message.text or "").split(maxsplit=1) if message else []
        data = callback.data or "" if callback else ""
        photo = None
        banner_role = _callback_banner(data) if callback else None
        share_id = None
        text = ""
        markup = None
        document = None
        try:
            upload = await attendance_upload(sessionmaker, bot, user.id, message, settings)
            async with sessionmaker() as session, session.begin():
                stored_update = await session.get(
                    TelegramUpdate, event_update.update_id, with_for_update=True
                )
                if stored_update is not None and (
                    stored_update.status == "done"
                    or (inbox_attempt is not None and stored_update.attempts != inbox_attempt)
                ):
                    return
                service = ClubService(session)
                person = await service.get_or_create_person(user.id, user.full_name, user.username)
                state = await _conversation(session, user.id)
                start_event = (
                    message
                    and len(command) == 2
                    and command[0].split("@", 1)[0] == "/start"
                    and command[1].startswith("e_")
                )
                flow = None
                start_qr = (
                    message
                    and len(command) == 2
                    and command[0].split("@", 1)[0] == "/start"
                    and command[1].startswith("a_")
                )
                if start_qr:
                    _reset(state)
                    flow = await handle_attendance(
                        session, user.id, state, None, f"aq:{command[1][2:]}", settings
                    )
                elif start_event:
                    _reset(state)
                    flow = await handle_events(
                        session, user.id, state, None, f"event:{command[1][2:]}:link", settings
                    )
                elif not command or not command[0].startswith("/"):
                    flow = await handle_identity(
                        session, user.id, state, message, data, settings, person
                    )
                    if flow is None:
                        flow = await handle_attendance(
                            session, user.id, state, message, data, settings, upload=upload
                        )
                    if flow is None:
                        flow = await handle_calendar(
                            session, user.id, state, message, data, settings
                        )
                    if flow is None:
                        flow = await handle_templates(
                            session, user.id, state, message, data, settings
                        )
                    if flow is None:
                        flow = await handle_events(session, user.id, state, message, data, settings)
                if flow is not None:
                    document = flow.document
                    text, markup, photo, banner_role = (
                        flow.text,
                        flow.markup,
                        flow.photo,
                        flow.banner_role,
                    )
                elif message and command and command[0].split("@", 1)[0] == "/start":
                    _reset(state)
                    parts = message.text.split(maxsplit=1)
                    if len(parts) == 2 and parts[1].startswith("c_"):
                        text, markup, photo = await _club_card(service, user.id, parts[1][2:])
                        banner_role = "clubs"
                    else:
                        text, markup = await _menu(service, user.id)
                        banner_role = "menu"
                elif message and message.text in {"/cancel", "/menu"}:
                    _reset(state)
                    text, markup = await _menu(service, user.id)
                    banner_role = "menu"
                elif message and state.state == "club_profile_edit":
                    banner_role = "club-owner"
                    payload = state.payload
                    club = await _profile_access(service, user.id, payload["club"])
                    try:
                        value = parse_value(payload["field"], message)
                    except ValidationError as error:
                        text = escape(str(error)) + "\n\n" + _club_edit_hint(payload["field"])
                        markup = _club_edit_navigation(payload)
                    else:
                        preview = dict(club)
                        preview[payload["field"]] = value
                        state.state = "club_profile_preview"
                        state.payload = {**payload, "value": value}
                        text, photo = (
                            "<b>Предпросмотр</b>\n\n" + card_text(preview),
                            preview.get("cover"),
                        )
                        markup = _confirmation("save", state)
                elif message and state.state == "organizer_add":
                    banner_role = "club-owner"
                    public_id = state.payload["club"]
                    await _leader_access(service, user.id, public_id)
                    raw = (message.text or "").strip()
                    target = await service.resolve_telegram_target(raw)
                    state.state = "organizer_preview"
                    state.payload = {
                        "club": public_id,
                        "target": target["telegram_id"],
                        "target_tag": raw if raw.startswith("@") else None,
                        "token": uuid4().hex,
                    }
                    text = (
                        f"Добавить организатора {escape(target['full_name'])} · "
                        f"{target['telegram_id']} со всеми тремя правами?"
                    )
                    markup = _confirmation("orgsave", state)
                elif message:
                    if state.state in {"club_profile_preview", "organizer_preview"}:
                        text = "Сначала сохрани или отмени подготовленное изменение."
                        save_action = "save" if state.state == "club_profile_preview" else "orgsave"
                        markup = _confirmation(save_action, state)
                        banner_role = "club-owner"
                    else:
                        saved = await session.get(UIScreen, (bot.id, user.id))
                        if saved:
                            text = saved.text
                            markup = (
                                InlineKeyboardMarkup.model_validate(saved.base_markup)
                                if saved.base_markup
                                else None
                            )
                            photo = saved.photo_file_id if saved.kind == "photo" else None
                            banner_role = saved.banner_role
                        else:
                            text, markup = await _menu(service, user.id)
                            banner_role = "menu"
                elif data.startswith("cancel:") and data != f"cancel:{state.payload.get('token')}":
                    text = "Эта кнопка относится к другому изменению. Продолжи текущий диалог."
                elif data in {"menu", "cancel"} or data.startswith("cancel:"):
                    public_id = state.payload.get("club")
                    previous_state = state.state
                    _reset(state)
                    if data.startswith("cancel") and public_id:
                        if previous_state.startswith("club_profile"):
                            text, markup = await _edit_menu(service, user.id, public_id)
                        elif previous_state.startswith("organizer"):
                            text, markup = await _organizers(service, user.id, public_id)
                        else:
                            text = "Изменение отменено."
                            markup = keyboard([("Назад", f"manageclub:{public_id}")])
                    else:
                        text, markup = await _menu(service, user.id)
                elif data == "clubs":
                    text, markup = await _clubs(service, manage=False, user_id=user.id)
                elif data == "manage":
                    text, markup = await _clubs(service, manage=True, user_id=user.id)
                elif data == "admin":
                    if not await service.is_admin(user.id):
                        raise PermissionDenied("Администрирование доступно только администраторам.")
                    text = legacy.text("admin_menu_text") + (
                        "\n\nВыбери клуб для изменения профиля и организаторов."
                    )
                    _, markup = await _clubs(service, manage=True, user_id=user.id)
                    markup.inline_keyboard.insert(
                        0, [InlineKeyboardButton(text="Создать клуб", callback_data="adminnew")]
                    )
                    markup.inline_keyboard.insert(
                        1, [InlineKeyboardButton(text="Календарь", callback_data="calendar")]
                    )
                elif data == "profile":
                    text = legacy.personal_account_text(person["full_name"]) + (
                        "\n\nПочта не подтверждена."
                    )
                    markup = keyboard([("В меню", "menu")])
                elif data.startswith("future:"):
                    text, markup = UNAVAILABLE, keyboard([("В меню", "menu")])
                elif data in {"faq", "faq:levels"}:
                    text = (
                        "<b>Как пользоваться ботом</b>\nОткрой «Клубы ЦУ», чтобы найти карточку "
                        "и подписаться на анонсы. В разделе «Мероприятия» можно выбрать встречу "
                        "и записаться после подтверждения студенческой почты.\n\n"
                        "<b>Уровни клубов</b>\nConcept → Pre-Seed → Seed → Pre-IPO → IPO. "
                        "Уровень назначает администрация. Гостевой вход планируется от Seed; "
                        "он пока не подключён."
                    )
                    markup = keyboard([(legacy.text("cu_clubs"), "clubs"), ("В меню", "menu")])
                elif data.startswith("club:"):
                    text, markup, photo = await _club_card(service, user.id, data.split(":", 1)[1])
                elif data.startswith("subscribe:"):
                    _, public_id, enabled = data.split(":")
                    if enabled not in {"0", "1"}:
                        raise ValidationError("Некорректное действие подписки.")
                    await service.set_subscription(user.id, public_id, enabled == "1")
                    text, markup, photo = await _club_card(service, user.id, public_id)
                elif data.startswith("share:"):
                    public_id = data.split(":", 1)[1]
                    if await service.get_club(public_id) is None:
                        raise ClubNotFound("Клуб не найден.")
                    share_id = public_id
                elif data.startswith("manageclub:"):
                    public_id = data.split(":", 1)[1]
                    permissions = await service.permissions(user.id, public_id)
                    if not any(permissions.values()):
                        raise PermissionDenied("Нет доступа к управлению этим клубом.")
                    club = await service.get_club(public_id)
                    if club is None:
                        raise ClubNotFound("Клуб не найден.")
                    owners = await service.organizer_names(user.id, public_id)
                    text, rows = legacy.owner_club_text(club, owners), []
                    text += f"\n\n<b>Уровень:</b> {escape(club['level'])}"
                    photo = club.get("cover")
                    if (
                        permissions["is_admin"]
                        or permissions["is_leader"]
                        or permissions["edit_profile"]
                    ):
                        rows.append([(legacy.text("profile"), f"editmenu:{public_id}")])
                    if permissions["is_admin"] or permissions["is_leader"]:
                        rows.append([("Организаторы", f"organizers:{public_id}")])
                    if permissions["manage_events"]:
                        rows.append([("Создать встречу", f"eventnew:{public_id}")])
                        rows.append([("Черновики", f"eventdrafts:{public_id}")])
                        rows.append([("Шаблоны", f"tmpls:{public_id}")])
                    if permissions["manage_events"] or permissions["record_attendance"]:
                        rows.append([("Встречи клуба", f"eventlist:{public_id}")])
                    if permissions["is_admin"]:
                        rows.append([("Изменить уровень", f"adminlevel:{public_id}")])
                        rows.append([("Назначить лидера", f"adminleader:{public_id}")])
                    rows.append([("В меню", "menu")])
                    markup = keyboard(*rows)
                elif data.startswith("editmenu:"):
                    _reset(state)
                    text, markup = await _edit_menu(service, user.id, data.split(":", 1)[1])
                elif data.startswith("edit:"):
                    parts = data.split(":")
                    _, public_id, field = parts[:3]
                    sequence = len(parts) == 4 and parts[3] == "seq"
                    club = await _profile_access(service, user.id, public_id)
                    if field not in FIELDS:
                        raise ValidationError("Неизвестное поле карточки.")
                    state.state = "club_profile_edit"
                    state.payload = {
                        "club": public_id,
                        "field": field,
                        "version": club["version"],
                        "token": uuid4().hex,
                        "sequence": sequence,
                    }
                    text = _club_edit_hint(field)
                    markup = _club_edit_navigation(state.payload)
                elif (
                    data == f"save:{state.payload.get('token')}"
                    and state.state == "club_profile_preview"
                ):
                    payload = state.payload
                    club = await service.update_club(
                        user.id,
                        payload["club"],
                        payload["field"],
                        payload["value"],
                        expected_version=payload["version"],
                    )
                    success_key = {
                        "name": "club_name_set",
                        "description": "club_description_set",
                        "channel": "club_link_set",
                        "cover": "club_avatar_set",
                    }.get(payload["field"])
                    success = (
                        legacy.text(success_key) if success_key else "Публичная команда сохранена."
                    )
                    _reset(state)
                    text, photo = success + "\n\n" + card_text(club), club.get("cover")
                    fields = list(FIELDS)
                    index = fields.index(payload["field"]) + 1
                    rows = []
                    if index < len(fields):
                        next_field = fields[index]
                        rows.append(
                            [
                                (
                                    f"Далее: {FIELDS[next_field]}",
                                    f"edit:{club['public_id']}:{next_field}:seq",
                                )
                            ]
                        )
                    rows += [
                        [("Изменить другое поле", f"editmenu:{club['public_id']}")],
                        [("Завершить редактирование", f"manageclub:{club['public_id']}")],
                    ]
                    markup = keyboard(*rows)
                elif data.startswith("organizers:"):
                    text, markup = await _organizers(service, user.id, data.split(":", 1)[1])
                elif data.startswith("orgadd:"):
                    public_id = data.split(":", 1)[1]
                    await _leader_access(service, user.id, public_id)
                    state.state, state.payload = (
                        "organizer_add",
                        {
                            "club": public_id,
                            "token": uuid4().hex,
                        },
                    )
                    text = (
                        "Пришли @тег или числовой Telegram ID. "
                        "Человек должен сначала открыть этого бота."
                    )
                    markup = keyboard([("Отмена", f"cancel:{state.payload['token']}")])
                elif (
                    data == f"orgsave:{state.payload.get('token')}"
                    and state.state == "organizer_preview"
                ):
                    public_id, target = state.payload["club"], state.payload["target"]
                    if state.payload.get("target_tag"):
                        resolved = await service.resolve_telegram_target(
                            state.payload["target_tag"]
                        )
                        if resolved["telegram_id"] != target:
                            raise ValidationError("Тег изменился. Выбери организатора заново.")
                    await service.set_organizer(user.id, public_id, target)
                    _reset(state)
                    text, markup = await _organizers(service, user.id, public_id)
                    text = "Организатор добавлен.\n\n" + text
                elif data.startswith("org:"):
                    _, public_id, target = data.split(":")
                    text, markup = await _org_detail(service, user.id, public_id, int(target))
                elif data.startswith("orgset:"):
                    _, public_id, target, mask = data.split(":")
                    if len(mask) != 3 or any(bit not in "01" for bit in mask):
                        raise ValidationError("Некорректные права организатора.")
                    await service.set_organizer(
                        user.id,
                        public_id,
                        int(target),
                        edit_profile=mask[0] == "1",
                        manage_events=mask[1] == "1",
                        record_attendance=mask[2] == "1",
                    )
                    text, markup = await _org_detail(service, user.id, public_id, int(target))
                elif data.startswith("orgremove:"):
                    _, public_id, target = data.split(":")
                    await _leader_access(service, user.id, public_id)
                    text = f"Удалить организатора {escape(target)} из клуба?"
                    markup = keyboard(
                        [("Да, удалить", f"orgdelete:{public_id}:{target}")],
                        [("Отмена", f"organizers:{public_id}")],
                    )
                elif data.startswith("orgdelete:"):
                    _, public_id, target = data.split(":")
                    await service.remove_organizer(user.id, public_id, int(target))
                    text, markup = await _organizers(service, user.id, public_id)
                else:
                    text = "Это действие уже завершено или устарело. Открой актуальный раздел."
                    markup = keyboard([("В меню", "menu")])
                # Consume durable input in the SAME transaction as its business/state
                # changes. A later Telegram failure must not apply a wizard answer
                # again to its next field. The next user action can redraw the screen.
                if stored_update is not None and stored_update.status == "processing":
                    stored_update.status = "done"
                    stored_update.lease_until = None
                    stored_update.last_error = None
        except (PermissionDenied, ClubNotFound, ValidationError) as exc:
            photo = None
            text, markup = escape(str(exc)), keyboard([("В меню", "menu")])
        except (ValueError, KeyError):
            photo = None
            text = "Не удалось выполнить это действие. Открой актуальный раздел."
            markup = keyboard([("В меню", "menu")])
        # No network requests happen inside the database transaction.
        if share_id:
            me = await bot.me()
            link = f"https://t.me/{me.username}?start=c_{share_id}"
            text = f"Постоянная ссылка на клуб:\n{escape(link)}"
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Поделиться",
                            url="https://t.me/share/url?" + urlencode({"url": link}),
                        ),
                    ],
                    [InlineKeyboardButton(text="Карточка клуба", callback_data=f"club:{share_id}")],
                ]
            )
        original_banner = photo is None and banner_role is not None
        if original_banner:
            photo = legacy.banner(banner_role)
        await manager.show(
            user.id,
            text,
            markup,
            photo,
            banner_role=banner_role if original_banner else None,
            trigger=event,
        )
        if callback:
            await callback.answer()
        if document is not None:
            await bot.send_document(user.id, document)

    router.message.register(dispatch)
    router.callback_query.register(dispatch)
    dispatcher.include_router(router)
    return dispatcher
