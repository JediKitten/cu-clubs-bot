"""Private-chat Telegram adapter; dialogue state lives in PostgreSQL, not aiogram FSM."""

from html import escape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from cu_clubs.models import Conversation
from cu_clubs.services import ClubNotFound, ClubService, PermissionDenied, ValidationError

UNAVAILABLE = "Этот раздел ещё в разработке. Сейчас доступны клубы и управление профилем клуба."
FIELDS = {
    "name": "Название",
    "description": "Описание",
    "channel": "Канал или сайт",
    "cover": "Обложка",
    "public_team": "Публичная команда",
}


def keyboard(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
            for row in rows
        ]
    )


def card_text(club: dict[str, Any]) -> str:
    team = club.get("public_team") or []
    people = (
        "\n".join(
            f"• {escape(member['name'])}"
            + (f" — {escape(member['role'])}" if member.get("role") else "")
            for member in team
        )
        or "Команда пока не указана"
    )
    return (
        f"<b>{escape(club['name'])}</b> · Уровень: {escape(club['level'])}\n\n"
        f"{escape(club.get('description') or 'Описание клуба заполняет команда.')}\n\n"
        f"<b>Команда</b>\n{people}"
        + (f"\n\nКанал или сайт: {escape(club['channel'])}" if club.get("channel") else "")
        + "".join(
            f"\n{escape(link['label'])}: {escape(link['url'])}" for link in club.get("links", [])
        )
        + "\n\nБлижайшие встречи появятся после подключения событий."
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


def _confirmation(action: str, state: Conversation) -> InlineKeyboardMarkup:
    token = state.payload["token"]
    return keyboard([("Сохранить", f"{action}:{token}"), ("Отмена", f"cancel:{token}")])


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
        [("Мероприятия", "future:events"), ("Клубы", "clubs")],
        [("Мои встречи", "future:mine"), ("Мой профиль", "profile")],
        [("FAQ", "faq")],
    ]
    if await service.managed_clubs(user_id):
        rows.append([("Управление клубом", "manage")])
    if await service.is_admin(user_id):
        rows.append([("Администрирование", "admin")])
    return "Здесь можно найти клуб и узнать о его команде. Выбери раздел:", keyboard(*rows)


async def _clubs(service: ClubService, *, manage: bool, user_id: int) -> tuple[str, Any]:
    clubs = await service.managed_clubs(user_id) if manage else await service.list_clubs()
    prefix = "manageclub" if manage else "club"
    rows = [[(club["name"], f"{prefix}:{club['public_id']}")] for club in clubs]
    rows.append([("В меню", "menu")])
    return ("Выбери клуб:" if clubs else "Клубы пока не добавлены."), keyboard(*rows)


async def _club_card(service: ClubService, user_id: int, public_id: str) -> tuple[str, Any, Any]:
    club = await service.get_club(public_id)
    if club is None:
        raise ClubNotFound("Клуб не найден.")
    subscribed = await service.get_subscription(user_id, public_id)
    return (
        card_text(club),
        keyboard(
            [
                (
                    "Отписаться" if subscribed else "Подписаться",
                    f"subscribe:{public_id}:{0 if subscribed else 1}",
                )
            ],
            [("Поделиться", f"share:{public_id}"), ("Что даёт уровень?", "faq:levels")],
            [("Клубы", "clubs"), ("В меню", "menu")],
        ),
        club.get("cover"),
    )


async def _edit_menu(service: ClubService, user_id: int, public_id: str) -> tuple[str, Any]:
    club = await _profile_access(service, user_id, public_id)
    return f"Профиль клуба «{escape(club['name'])}». Что изменить?", keyboard(
        *[[(name, f"edit:{public_id}:{field}")] for field, name in FIELDS.items()],
        [("Посмотреть карточку", f"club:{public_id}")],
        [("Назад", f"manageclub:{public_id}"), ("В меню", "menu")],
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
    del settings  # Configuration and credentials belong to the application lifespan.
    dispatcher = Dispatcher(disable_fsm=True)
    router = Router(name="private_clubs")
    router.message.filter(F.chat.type == "private")
    router.callback_query.filter(F.message.chat.type == "private")

    async def dispatch(event: Message | CallbackQuery, bot: Bot) -> None:
        user = event.from_user
        if user is None:
            return
        callback = event if isinstance(event, CallbackQuery) else None
        message = event if isinstance(event, Message) else None
        command = (message.text or "").split(maxsplit=1) if message else []
        data = callback.data or "" if callback else ""
        photo = None
        share_id = None
        text = ""
        markup = None
        try:
            async with sessionmaker() as session, session.begin():
                service = ClubService(session)
                person = await service.get_or_create_person(user.id, user.full_name, user.username)
                state = await _conversation(session, user.id)
                if message and command and command[0].split("@", 1)[0] == "/start":
                    _reset(state)
                    parts = message.text.split(maxsplit=1)
                    if len(parts) == 2 and parts[1].startswith("c_"):
                        text, markup, photo = await _club_card(service, user.id, parts[1][2:])
                    else:
                        text, markup = await _menu(service, user.id)
                elif message and message.text in {"/cancel", "/menu"}:
                    _reset(state)
                    text, markup = await _menu(service, user.id)
                elif message and state.state == "club_profile_edit":
                    payload = state.payload
                    club = await _profile_access(service, user.id, payload["club"])
                    value = parse_value(payload["field"], message)
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
                    public_id = state.payload["club"]
                    await _leader_access(service, user.id, public_id)
                    raw = (message.text or "").strip()
                    if not raw.isascii() or not raw.isdigit() or not 0 < int(raw) < 2**63:
                        raise ValidationError("Пришли положительный числовой Telegram ID.")
                    state.state = "organizer_preview"
                    state.payload = {"club": public_id, "target": int(raw), "token": uuid4().hex}
                    text = f"Добавить организатора {raw} со всеми тремя правами?"
                    markup = _confirmation("orgsave", state)
                elif message:
                    if state.state in {"club_profile_preview", "organizer_preview"}:
                        text = "Сначала сохрани или отмени подготовленное изменение."
                        save_action = "save" if state.state == "club_profile_preview" else "orgsave"
                        markup = _confirmation(save_action, state)
                    else:
                        text, markup = await _menu(service, user.id)
                elif data.startswith("cancel:") and data != f"cancel:{state.payload.get('token')}":
                    text = "Эта кнопка относится к другому изменению. Продолжи текущий диалог."
                elif data in {"menu", "cancel"} or data.startswith("cancel:"):
                    public_id = state.payload.get("club")
                    _reset(state)
                    if data.startswith("cancel") and public_id:
                        text = "Изменение отменено."
                        markup = keyboard([("Управление клубом", f"manageclub:{public_id}")])
                    else:
                        text, markup = await _menu(service, user.id)
                elif data == "clubs":
                    text, markup = await _clubs(service, manage=False, user_id=user.id)
                elif data == "manage":
                    text, markup = await _clubs(service, manage=True, user_id=user.id)
                elif data == "admin":
                    if not await service.is_admin(user.id):
                        raise PermissionDenied("Администрирование доступно только администраторам.")
                    text = "Администрирование: выбери клуб для изменения профиля и организаторов."
                    _, markup = await _clubs(service, manage=True, user_id=user.id)
                elif data == "profile":
                    text = (
                        f"<b>Мой профиль</b>\n{escape(person['full_name'])}\nПочта не подтверждена."
                    )
                    markup = keyboard([("В меню", "menu")])
                elif data.startswith("future:"):
                    text, markup = UNAVAILABLE, keyboard([("В меню", "menu")])
                elif data in {"faq", "faq:levels"}:
                    text = (
                        "<b>Как пользоваться ботом</b>\nОткрой «Клубы», чтобы найти карточку "
                        "и подписаться на анонсы. События и запись пока в разработке.\n\n"
                        "<b>Уровни клубов</b>\nConcept → Pre-Seed → Seed → Pre-IPO → IPO. "
                        "Уровень назначает администрация. Гостевой вход планируется от Seed; "
                        "он пока не подключён."
                    )
                    markup = keyboard([("Клубы", "clubs"), ("В меню", "menu")])
                elif data.startswith("club:"):
                    text, markup, photo = await _club_card(service, user.id, data.split(":", 1)[1])
                elif data.startswith("subscribe:"):
                    _, public_id, enabled = data.split(":")
                    if enabled not in {"0", "1"}:
                        raise ValidationError("Некорректное действие подписки.")
                    await service.set_subscription(user.id, public_id, enabled == "1")
                    card, markup, photo = await _club_card(service, user.id, public_id)
                    text = "Подписка включена." if enabled == "1" else "Подписка отключена."
                    text += "\n\n" + card
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
                    text, rows = f"Управление: {escape(club['name'])}", []
                    if (
                        permissions["is_admin"]
                        or permissions["is_leader"]
                        or permissions["edit_profile"]
                    ):
                        rows.append([("Профиль клуба", f"editmenu:{public_id}")])
                    if permissions["is_admin"] or permissions["is_leader"]:
                        rows.append([("Организаторы", f"organizers:{public_id}")])
                    rows.append([("В меню", "menu")])
                    markup = keyboard(*rows)
                elif data.startswith("editmenu:"):
                    text, markup = await _edit_menu(service, user.id, data.split(":", 1)[1])
                elif data.startswith("edit:"):
                    _, public_id, field = data.split(":")
                    club = await _profile_access(service, user.id, public_id)
                    if field not in FIELDS:
                        raise ValidationError("Неизвестное поле карточки.")
                    state.state = "club_profile_edit"
                    state.payload = {
                        "club": public_id,
                        "field": field,
                        "version": club["version"],
                        "token": uuid4().hex,
                    }
                    hints = {
                        "public_team": "Пришли список: один человек на строку, «Имя — роль». "
                        "Это публичное описание, оно не выдаёт права. Для очистки — «Удалить».",
                        "cover": "Пришли фотографию для обложки или «Удалить».",
                        "channel": "Пришли ссылку https://… или @username; "
                        "для очистки — «Удалить».",
                    }
                    text = hints.get(field, f"Пришли новое значение: {FIELDS[field].lower()}.")
                    markup = keyboard([("Отмена", f"cancel:{state.payload['token']}")])
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
                    _reset(state)
                    text, photo = "Профиль сохранён.\n\n" + card_text(club), club.get("cover")
                    markup = keyboard([("Профиль клуба", f"editmenu:{club['public_id']}")])
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
                    text = "Пришли числовой Telegram ID. Человек должен сначала открыть этого бота."
                    markup = keyboard([("Отмена", f"cancel:{state.payload['token']}")])
                elif (
                    data == f"orgsave:{state.payload.get('token')}"
                    and state.state == "organizer_preview"
                ):
                    public_id, target = state.payload["club"], state.payload["target"]
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
        if photo:
            await bot.send_photo(user.id, photo)
        if text:
            await _send_text(bot, user.id, text, markup)
        if callback:
            await callback.answer()

    router.message.register(dispatch)
    router.callback_query.register(dispatch)
    dispatcher.include_router(router)
    return dispatcher
