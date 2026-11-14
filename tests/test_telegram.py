"""Offline handler interactions: no Telegram API, credentials, or external services."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageMedia,
    EditMessageText,
    GetMe,
    SendMessage,
    SendPhoto,
)
from aiogram.types import BufferedInputFile, Update, User

from cu_clubs import legacy, telegram
from cu_clubs.services import PermissionDenied, ValidationError


class MemoryDB:
    def __init__(self):
        self.active = False
        self.commits = 0
        self.states = {}
        self.club = {
            "public_id": "cinema",
            "name": "Киноклуб",
            "level": "Seed",
            "description": "",
            "channel": None,
            "cover": None,
            "public_team": [],
            "version": 1,
        }
        self.people = {}
        self.admins = {103}
        self.leaders = {101}
        self.flags = {
            102: {"edit_profile": True, "manage_events": False, "record_attendance": False}
        }
        self.subscriptions = set()

    def session(self):
        return MemorySession(self)


class MemorySession:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def begin(self):
        return MemoryTransaction(self.db)

    async def get(self, model, key, **kwargs):
        return None


class MemoryTransaction:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        assert not self.db.active
        self.snapshot = deepcopy(self.db.__dict__)
        self.db.active = True

    async def __aexit__(self, error_type, *args):
        if error_type:
            self.db.__dict__.update(self.snapshot)
        else:
            self.db.commits += 1
        self.db.active = False


async def memory_conversation(session, user_id):
    assert session.db.active
    return session.db.states.setdefault(user_id, SimpleNamespace(state="idle", payload={}))


class MemoryService:
    def __init__(self, session):
        self.db = session.db
        assert self.db.active

    async def resolve_telegram_target(self, value):
        raw = str(value).strip()
        if raw.startswith("@"):
            ids = [
                key
                for key, person in self.db.people.items()
                if (person.get("username") or "").lower() == raw[1:].lower()
            ]
        else:
            ids = (
                [int(raw)] if raw.isascii() and raw.isdigit() and int(raw) in self.db.people else []
            )
        if len(ids) != 1:
            raise ValidationError(
                "Пришли известный @тег или числовой Telegram ID; человек должен открыть бота."
            )
        person = self.db.people[ids[0]]
        return {
            "telegram_id": ids[0],
            "full_name": person["full_name"],
            "username": person.get("username"),
        }

    async def get_or_create_person(self, user_id, full_name, username):
        self.db.people[user_id] = {"full_name": full_name, "username": username}
        return self.db.people[user_id]

    async def is_admin(self, user_id):
        return user_id in self.db.admins

    async def permissions(self, user_id, public_id):
        if public_id != "cinema":
            raise ValidationError("Клуб не найден")
        return {
            "is_admin": user_id in self.db.admins,
            "is_leader": user_id in self.db.leaders,
            **self.db.flags.get(
                user_id, {"edit_profile": False, "manage_events": False, "record_attendance": False}
            ),
        }

    async def list_clubs(self):
        return [deepcopy(self.db.club)]

    async def get_club(self, public_id):
        return deepcopy(self.db.club) if public_id == "cinema" else None

    async def managed_clubs(self, user_id):
        return (
            await self.list_clubs()
            if any((await self.permissions(user_id, "cinema")).values())
            else []
        )

    async def get_subscription(self, user_id, public_id):
        assert public_id == "cinema"
        return user_id in self.db.subscriptions

    async def set_subscription(self, user_id, public_id, enabled):
        assert public_id == "cinema"
        if enabled:
            self.db.subscriptions.add(user_id)
        else:
            self.db.subscriptions.discard(user_id)

    async def update_club(self, user_id, public_id, field, value, *, expected_version=None):
        p = await self.permissions(user_id, public_id)
        if not (p["is_admin"] or p["is_leader"] or p["edit_profile"]):
            raise PermissionDenied("Нет права редактировать профиль")
        if expected_version != self.db.club["version"]:
            raise ValidationError("Карточка изменилась. Открой актуальную версию")
        self.db.club[field] = deepcopy(value)
        self.db.club["version"] += 1
        return deepcopy(self.db.club)

    async def _team_access(self, user_id, public_id):
        p = await self.permissions(user_id, public_id)
        if not (p["is_admin"] or p["is_leader"]):
            raise PermissionDenied("Организаторами управляют лидер и администрация")

    async def organizer_names(self, user_id, public_id):
        if not any((await self.permissions(user_id, public_id)).values()):
            raise PermissionDenied("Нет доступа к управлению этим клубом.")
        return [
            deepcopy(
                self.db.people.get(
                    person_id,
                    {
                        "full_name": str(person_id),
                        "username": f"user_{person_id}",
                    },
                )
            )
            for person_id in sorted(self.db.leaders | self.db.flags.keys())
        ]

    async def list_organizers(self, user_id, public_id):
        await self._team_access(user_id, public_id)
        return [
            {"telegram_id": person_id, "full_name": str(person_id), "role": "organizer", **flags}
            for person_id, flags in self.db.flags.items()
        ]

    async def set_organizer(self, user_id, public_id, target, **flags):
        await self._team_access(user_id, public_id)
        if target not in self.db.people:
            raise ValidationError("Пользователь должен сначала открыть бота")
        self.db.flags[target] = flags or {
            "edit_profile": True,
            "manage_events": True,
            "record_attendance": True,
        }

    async def remove_organizer(self, user_id, public_id, target):
        await self._team_access(user_id, public_id)
        self.db.flags.pop(target, None)


class FakeBot(Bot):
    def __init__(self, db, bot_id=123456):
        super().__init__(f"{bot_id}:OFFLINE_TEST_ONLY_TOKEN")
        self.db = db
        self.sent = []
        self.answers = []
        self.deleted = []

    async def __call__(self, method, request_timeout=None):
        assert not self.db.active, "Telegram requests must happen after the DB transaction"
        if isinstance(method, GetMe):
            return User(id=123456, is_bot=True, first_name="Test", username="offline_test_bot")
        if isinstance(method, AnswerCallbackQuery):
            self.answers.append(method.callback_query_id)
            return True
        if isinstance(method, DeleteMessage):
            self.deleted.append(method.message_id)
            return True
        assert isinstance(method, SendMessage | SendPhoto | EditMessageMedia | EditMessageText), (
            type(method)
        )
        assert self.db.commits > 0
        self.sent.append(method)
        return SimpleNamespace(
            message_id=getattr(method, "message_id", None) or len(self.sent),
            photo=[SimpleNamespace(file_id=f"OFFLINE_PHOTO_{self.id}")],
        )

    @property
    def last(self):
        return self.sent[-1]

    @property
    def last_text(self):
        method = self.last
        if isinstance(method, EditMessageMedia):
            return method.media.caption
        return method.caption if isinstance(method, SendPhoto) else method.text

    def action(self, prefix):
        return next(
            button.callback_data
            for row in self.last.reply_markup.inline_keyboard
            for button in row
            if (button.callback_data or "").startswith(prefix)
        )


class Interaction:
    def __init__(self, db, bot=None):
        self.db = db
        self.bot = bot or FakeBot(db)
        self.dp = telegram.build_dispatcher(db.session, SimpleNamespace())
        self.sequence = 0

    async def send(self, *, text=None, callback=None, user_id=101, chat_type="private", photo=None):
        self.sequence += 1
        user = {"id": user_id, "is_bot": False, "first_name": "<Actor>", "username": "any_name"}
        message = {
            "message_id": self.sequence,
            "date": datetime.now(UTC),
            "chat": {"id": user_id, "type": chat_type},
            "from": user,
        }
        if text is not None:
            message["text"] = text
        if photo:
            message["photo"] = [
                {"file_id": photo, "file_unique_id": "unique", "width": 10, "height": 10}
            ]
        payload = {"update_id": self.sequence}
        if callback:
            payload["callback_query"] = {
                "id": str(self.sequence),
                "from": user,
                "chat_instance": "test",
                "message": message,
                "data": callback,
            }
        else:
            payload["message"] = message
        await self.dp.feed_update(self.bot, Update.model_validate(payload))
        return self.bot.last if self.bot.sent else None


@pytest.fixture
def interaction(monkeypatch):
    monkeypatch.setattr(telegram, "ClubService", MemoryService)
    monkeypatch.setattr(telegram, "_conversation", memory_conversation)

    # These focused handler tests use an in-memory domain. The real durable renderer
    # and its concurrency/restart behavior are exercised in test_screens and below.
    class HandlerRenderer:
        def __init__(self, sessions, bot, file_ids):
            self.bot, self.file_ids = bot, file_ids

        async def handle_page(self, callback):
            return False

        async def show(self, user_id, text, markup, photo, *, banner_role, trigger):
            await telegram._send_screen(
                self.bot,
                user_id,
                text,
                markup,
                photo,
                banner_role=banner_role,
                file_ids=self.file_ids,
            )

    async def separate_event_flow(*args, **kwargs):
        return None

    monkeypatch.setattr(telegram, "ScreenManager", HandlerRenderer)
    monkeypatch.setattr(telegram, "handle_events", separate_event_flow)
    return Interaction(MemoryDB())


def run(coroutine):
    return asyncio.run(coroutine)


def test_navigation_subscription_share_and_honest_profile(interaction):
    async def scenario():
        i = interaction
        await i.send(text="/start", user_id=104)
        assert i.bot.action("clubs") == "clubs"
        assert all(
            button.callback_data != "manage"
            for row in i.bot.last.reply_markup.inline_keyboard
            for button in row
        )
        await i.send(callback="clubs", user_id=104)
        assert i.bot.action("club:") == "club:cinema"
        await i.send(text="/start c_cinema", user_id=104)
        assert "Киноклуб" in i.bot.last_text
        await i.send(callback=i.bot.action("subscribe:"), user_id=104)
        assert 104 in i.db.subscriptions
        assert i.bot.last_text.startswith("Клуб: <b>Киноклуб</b>\nПодписка включена\n")
        await i.send(callback="club:cinema", user_id=104)
        assert i.bot.last_text.count("Подписка включена") == 1
        await i.send(callback=i.bot.action("subscribe:"), user_id=104)
        assert 104 not in i.db.subscriptions
        assert "Подписка включена" not in i.bot.last_text
        await i.send(callback="share:cinema", user_id=104)
        assert "https://t.me/offline_test_bot?start=c_cinema" in i.bot.last_text
        await i.send(callback="profile", user_id=104)
        assert "&lt;Actor&gt;" in i.bot.last_text
        assert "Почта не подтверждена" in i.bot.last_text
        await i.send(callback="future:survey", user_id=104)
        assert "ещё в разработке" in i.bot.last_text
        await i.send(callback="share:missing", user_id=104)
        assert "Постоянная ссылка" not in i.bot.last_text
        await i.send(callback="admin", user_id=104)
        assert "только администраторам" in i.bot.last_text

    run(scenario())


@pytest.mark.parametrize(
    "update,role,caption",
    [
        ({"text": "/start"}, "menu", "<b>Главное меню</b>"),
        ({"text": "/menu"}, "menu", "<b>Главное меню</b>"),
        ({"callback": "menu"}, "menu", "<b>Главное меню</b>"),
        ({"callback": "clubs"}, "clubs", "Список клубов ЦУ"),
        (
            {"callback": "manage"},
            "club-owner",
            "<b>Список ваших клубов</b>\n\n<i>Всего:</i> <b>1</b>",
        ),
        (
            {"callback": "manageclub:cinema"},
            "club-owner",
            "Клуб: <b>Киноклуб</b>\n\n<u>Организаторы:</u>\n"
            "- <b>&lt;Actor&gt;</b> (@any_name)\n- <b>102</b> (@user_102)\n"
            "\n<b>Описание:</b>\n<blockquote><i>Не указано</i></blockquote>"
            "\n\n<b>Ссылка на чат/канал клуба:</b>\nНе указано"
            "\n\n<b>Уровень:</b> Seed",
        ),
        (
            {"callback": "editmenu:cinema"},
            "club-owner",
            "<b>Настройка профиля клуба</b>\n\nКиноклуб",
        ),
        (
            {"callback": "edit:cinema:name"},
            "club-owner",
            "<b>Введите название клуба</b>",
        ),
        (
            {"callback": "profile"},
            "personal-account",
            "<i>Добро пожаловать &lt;Actor&gt;, ваша роль <b>не определена</b></i>"
            "\n\nПочта не подтверждена.",
        ),
        ({"callback": "future:events"}, "events", telegram.UNAVAILABLE),
        ({"callback": "future:mine"}, "personal-account", telegram.UNAVAILABLE),
    ],
)
def test_shared_screens_send_original_banner_and_exact_caption(interaction, update, role, caption):
    async def scenario():
        i = interaction
        await i.send(**update)
        assert len(i.bot.sent) == 1
        method = i.bot.last
        assert isinstance(method, SendPhoto)
        assert isinstance(method.photo, BufferedInputFile)
        assert method.photo.filename == f"{role}.png"
        assert method.photo.data == legacy.banner(role).data
        assert method.caption == caption
        assert method.parse_mode == "HTML"
        assert method.reply_markup

    run(scenario())


def test_original_banner_file_ids_are_reused_only_for_same_bot_and_role(interaction):
    class UploadBot(FakeBot):
        async def __call__(self, method, request_timeout=None):
            response = await super().__call__(method, request_timeout)
            if isinstance(method, SendPhoto):
                response.photo = [SimpleNamespace(file_id=f"UPLOAD_{self.id}_{len(self.sent)}")]
            return response

    async def scenario():
        i = interaction
        first = UploadBot(i.db)
        second = UploadBot(i.db, bot_id=654321)
        i.bot = first
        try:
            await i.send(text="/start")
            assert isinstance(first.last.photo, BufferedInputFile)
            assert first.last.photo.filename == "menu.png"
            await i.send(callback="menu")
            assert first.last.photo == "UPLOAD_123456_1"
            await i.send(callback="profile")
            assert isinstance(first.last.photo, BufferedInputFile)
            assert first.last.photo.filename == "personal-account.png"
            await i.send(callback="club:cinema")
            assert isinstance(first.last.photo, BufferedInputFile)
            assert first.last.photo.filename == "clubs.png"
            club_banner_id = f"UPLOAD_123456_{len(first.sent)}"
            i.db.club["cover"] = "CLUB_COVER_FILE_ID"
            await i.send(callback="club:cinema")
            assert first.last.photo == "CLUB_COVER_FILE_ID"
            i.db.club["cover"] = None
            await i.send(callback="club:cinema")
            assert first.last.photo == club_banner_id
            # The same dispatcher must upload the original for another bot.
            i.bot = second
            await i.send(callback="menu")
            assert isinstance(second.last.photo, BufferedInputFile)
            assert second.last.photo.filename == "menu.png"
            await i.send(callback="menu")
            assert second.last.photo == "UPLOAD_654321_1"
            # A fresh dispatcher uploads again; cache is not global or persistent.
            restarted = Interaction(i.db, first)
            await restarted.send(callback="menu")
            assert isinstance(first.last.photo, BufferedInputFile)
        finally:
            await first.session.close()
            await second.session.close()

    run(scenario())


def test_main_menu_uses_exact_original_labels_and_permission_gates(interaction):
    async def scenario():
        i = interaction
        for user_id, privileged in (
            (104, {}),
            (101, {"manage": "Мои клубы"}),
            (103, {"manage": "Мои клубы", "admin": "Админ-меню"}),
        ):
            await i.send(text="/start", user_id=user_id)
            buttons = {
                button.callback_data: button.text
                for row in i.bot.last.reply_markup.inline_keyboard
                for button in row
            }
            assert buttons == {
                "clubs": "Клубы ЦУ",
                "events": "Мероприятия",
                "profile": "Личный кабинет",
                "my_events": "Мои мероприятия",
                "faq": "FAQ",
                "supending": "Опросы после встреч",
                **privileged,
            }

    run(scenario())


def test_persistent_draft_preview_save_and_html_escape(interaction):
    async def scenario():
        i = interaction
        await i.send(callback="edit:cinema:description")
        restored = Interaction(i.db, i.bot)  # A fresh dispatcher has no FSM state.
        await restored.send(text="<b>Описание</b>")
        assert i.db.club["description"] == ""
        assert "&lt;b&gt;Описание&lt;/b&gt;" in i.bot.last_text
        action = i.bot.action("save:")
        restored_again = Interaction(i.db, i.bot)
        await restored_again.send(callback=action)
        assert i.db.club["description"] == "<b>Описание</b>"
        assert i.db.states[101].state == "idle"
        version = i.db.club["version"]
        await restored_again.send(callback=action)
        assert i.db.club["version"] == version
        assert "устарело" in i.bot.last_text

    run(scenario())


@pytest.mark.parametrize(
    "field,update,success",
    [
        ("name", {"text": "Новое имя"}, "<b>Название клуба успешно установлено ✅</b>"),
        ("description", {"text": "Описание"}, "<b>Описание клуба успешно установлено ✅</b>"),
        (
            "channel",
            {"text": "https://t.me/example"},
            "<b>Ссылка на канал/чат клуба успешно установлено ✅</b>",
        ),
        ("cover", {"photo": "ORIGINAL_CLUB_COVER"}, "<b>Аватар клуба успешно установлен ✅</b>"),
    ],
)
def test_profile_save_preserves_exact_original_success(interaction, field, update, success):
    async def scenario():
        i = interaction
        await i.send(callback=f"edit:cinema:{field}")
        await i.send(**update)
        await i.send(callback=i.bot.action("save:"))
        assert i.bot.last_text == success + "\n\n" + telegram.card_text(i.db.club)
        assert i.db.states[101].state == "idle"
        if field == "cover":
            assert isinstance(i.bot.last, SendPhoto)
            assert i.bot.last.photo == "ORIGINAL_CLUB_COVER"

    run(scenario())


def test_owner_card_uses_real_organizers_and_original_cover(interaction):
    async def scenario():
        i = interaction
        i.db.club["cover"] = "ORIGINAL_CLUB_COVER"
        i.db.club["public_team"] = [{"name": "Public-only actor", "role": "Режиссёр"}]
        # An organizer may read the owner card without leader-only team access.
        await i.send(callback="manageclub:cinema", user_id=102)
        assert isinstance(i.bot.last, SendPhoto)
        assert i.bot.last.photo == "ORIGINAL_CLUB_COVER"
        assert "<u>Организаторы:</u>" in i.bot.last.caption
        assert "- <b>&lt;Actor&gt;</b> (@any_name)" in i.bot.last.caption
        assert "Public-only actor" not in i.bot.last.caption
        assert "Режиссёр" not in i.bot.last.caption
        assert not any(
            button.callback_data == "organizers:cinema"
            for row in i.bot.last.reply_markup.inline_keyboard
            for button in row
        )

    run(scenario())


def test_revoked_permission_cannot_save_or_continue_draft(interaction):
    async def scenario():
        i = interaction
        await i.send(callback="edit:cinema:name", user_id=102)
        await i.send(text="Новое имя", user_id=102)
        action = i.bot.action("save:")
        del i.db.flags[102]
        await i.send(callback=action, user_id=102)
        assert "Нет права" in i.bot.last_text
        assert i.db.club["name"] == "Киноклуб"
        await i.send(callback="edit:cinema:name", user_id=102)
        assert "Нет права" in i.bot.last_text
        await i.send(callback="manageclub:cinema", user_id=102)
        assert "Нет доступа" in i.bot.last_text

    run(scenario())


def test_old_confirmation_and_concurrent_edit_do_not_overwrite(interaction):
    async def scenario():
        i = interaction
        await i.send(callback="edit:cinema:name")
        await i.send(text="Первое имя")
        old_save, old_cancel = i.bot.action("save:"), i.bot.action("cancel:")
        await i.send(callback="edit:cinema:description")
        await i.send(text="Новый текст")
        current_save = i.bot.action("save:")
        await i.send(callback=old_save)
        assert i.db.club["name"] == "Киноклуб"
        await i.send(callback=old_cancel)
        assert i.db.states[101].state == "club_profile_preview"
        i.db.club["version"] += 1
        await i.send(callback=current_save)
        assert "Карточка изменилась" in i.bot.last_text
        assert i.db.club["description"] == ""

    run(scenario())


def test_public_team_photo_and_cancel(interaction):
    async def scenario():
        i = interaction
        previous_flags = deepcopy(i.db.flags)
        await i.send(callback="edit:cinema:public_team")
        await i.send(text="Кир — ведущий\nАня — программа")
        await i.send(callback=i.bot.action("save:"))
        assert i.db.club["public_team"][0] == {"name": "Кир", "role": "ведущий"}
        assert i.db.flags == previous_flags
        await i.send(callback="edit:cinema:cover")
        await i.send(photo="OFFLINE_FILE_ID")
        assert isinstance(i.bot.last, SendPhoto)
        assert i.bot.last.photo == "OFFLINE_FILE_ID"
        assert "Предпросмотр" in i.bot.last.caption
        assert i.bot.last.reply_markup
        await i.send(callback=i.bot.action("save:"))
        assert i.db.club["cover"] == "OFFLINE_FILE_ID"
        await i.send(callback="edit:cinema:name")
        await i.send(text="Отменённое имя")
        await i.send(callback=i.bot.action("cancel:"))
        assert i.db.club["name"] == "Киноклуб"
        assert i.db.states[101].state == "idle"

    run(scenario())


def test_club_edit_continues_to_next_field_and_cancel_returns_to_editor(interaction):
    async def scenario():
        i = interaction
        await i.send(callback="edit:cinema:name")
        await i.send(text="Новый киноклуб")
        await i.send(callback=i.bot.action("save:"))
        assert i.bot.action("edit:") == "edit:cinema:description:seq"
        await i.send(callback=i.bot.action("edit:"))
        await i.send(text="x" * 3001)
        assert i.db.states[101].state == "club_profile_edit"
        assert i.bot.action("edit:") == "edit:cinema:name:seq"
        await i.send(text="Новое описание клуба")
        await i.send(callback=i.bot.action("cancel:"))
        assert i.db.club["name"] == "Новый киноклуб"
        assert i.db.club["description"] == ""
        assert i.bot.action("edit:") == "edit:cinema:name"
        assert i.bot.action("manageclub:") == "manageclub:cinema"

    run(scenario())


def test_organizers_require_numeric_id_and_leader_on_every_action(interaction):
    async def scenario():
        i = interaction
        await i.send(text="/start", user_id=105)
        await i.send(callback="orgadd:cinema", user_id=102)
        assert "лидер" in i.bot.last_text
        await i.send(callback="orgadd:cinema")
        await i.send(text="@some_name")
        assert "числовой" in i.bot.last_text
        await i.send(text="105")
        await i.send(callback=i.bot.action("orgsave:"))
        assert all(i.db.flags[105].values())
        await i.send(callback="orgset:cinema:105:010")
        assert i.db.flags[105] == {
            "edit_profile": False,
            "manage_events": True,
            "record_attendance": False,
        }
        await i.send(callback="orgdelete:cinema:105", user_id=102)
        assert 105 in i.db.flags
        await i.send(callback="orgremove:cinema:105")
        assert 105 in i.db.flags
        await i.send(callback="orgdelete:cinema:105")
        assert 105 not in i.db.flags

    run(scenario())


def test_group_updates_are_ignored_and_long_cards_fit_telegram(interaction):
    async def scenario():
        i = interaction
        await i.send(text="/start", chat_type="group")
        await i.send(callback="clubs", chat_type="supergroup")
        assert not i.bot.sent and not i.db.states
        i.db.club["description"] = "😀" * 6000 + "<end>"
        await i.send(callback="club:cinema")
        assert isinstance(i.bot.sent[0], SendPhoto)
        assert i.bot.sent[0].caption is None
        assert i.bot.sent[0].reply_markup is None
        messages = [item for item in i.bot.sent if isinstance(item, SendMessage)]
        assert len(messages) > 1
        assert "😀" * 6000 + "<end>" in "".join(item.text for item in messages)
        assert all(len(item.text.encode("utf-16-le")) // 2 <= 4096 for item in messages)
        assert messages[-1].reply_markup
        assert all(item.reply_markup is None for item in messages[:-1])

    run(scenario())


@pytest.fixture
def telegram_database_url():
    """A disposable schema; no application .env or real user identifiers are used."""
    import os
    from uuid import uuid4

    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from cu_clubs.models import Base

    configured = os.environ.get("TEST_DATABASE_URL")
    if not configured:
        pytest.skip("Set TEST_DATABASE_URL for durable Telegram-state PostgreSQL checks")
    schema = f"test_telegram_{uuid4().hex}"
    engine = create_engine(configured)
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            Base.metadata.create_all(connection)
        scoped = make_url(configured).update_query_dict({"options": f"-csearch_path={schema}"})
        yield scoped.render_as_string(hide_password=False)
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


@pytest.mark.integration
def test_real_postgres_dispatcher_restores_draft_and_rechecks_access(telegram_database_url):
    from pydantic import SecretStr
    from sqlalchemy import select

    from cu_clubs.config import Settings
    from cu_clubs.db import make_database
    from cu_clubs.models import Conversation
    from cu_clubs.services import ClubService

    async def scenario():
        settings = Settings(
            _env_file=None, environment="test", database_url=SecretStr(telegram_database_url)
        )
        engine, sessions = make_database(settings)
        try:
            async with sessions.begin() as session:
                service = ClubService(session)
                await service.bootstrap([103], 101)
                await service.get_or_create_person(102, "Editor")
                await service.set_organizer(
                    101,
                    "cinema",
                    102,
                    edit_profile=True,
                    manage_events=False,
                    record_attendance=False,
                )
                await service.get_or_create_person(104, "External")
                with pytest.raises(PermissionDenied):
                    await service.organizer_names(104, "cinema")
                names = await service.organizer_names(102, "cinema")
                assert len(names) == 2
                assert {"full_name": "Editor", "username": None} in names
                # Reading names does not grant organizer-management permissions.
                with pytest.raises(PermissionDenied):
                    await service.list_organizers(102, "cinema")
            tracker = MemoryDB()
            tracker.commits = 1
            i = Interaction(tracker)
            i.dp = telegram.build_dispatcher(sessions, settings)
            await i.send(callback="edit:cinema:description", user_id=102)
            # Reloading a dispatcher does not lose the persisted input prompt.
            i.dp = telegram.build_dispatcher(sessions, settings)
            await i.send(text="Saved across dispatcher restarts", user_id=102)
            action = i.bot.action("save:")
            async with sessions.begin() as session:
                state = await session.scalar(
                    select(Conversation).where(Conversation.telegram_id == 102)
                )
                assert state.state == "club_profile_preview"
                assert state.payload["value"] == "Saved across dispatcher restarts"
            i.dp = telegram.build_dispatcher(sessions, settings)
            await i.send(callback=action, user_id=102)
            async with sessions.begin() as session:
                assert (await ClubService(session).get_club("cinema"))["description"] == (
                    "Saved across dispatcher restarts"
                )
            await i.send(callback="edit:cinema:name", user_id=102)
            await i.send(text="Must never save", user_id=102)
            revoked_action = i.bot.action("save:")
            async with sessions.begin() as session:
                service = ClubService(session)
                await service.remove_organizer(101, "cinema", 102)
                with pytest.raises(PermissionDenied):
                    await service.organizer_names(102, "cinema")
            i.dp = telegram.build_dispatcher(sessions, settings)
            await i.send(callback=revoked_action, user_id=102)
            assert "Нет права" in i.bot.last_text
            async with sessions.begin() as session:
                assert (await ClubService(session).get_club("cinema"))["name"] == "Киноклуб"
        finally:
            await engine.dispose()

    run(scenario())
