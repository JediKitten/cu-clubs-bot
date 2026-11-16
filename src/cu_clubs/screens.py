"""One durable Telegram screen per bot/chat, serialized across worker processes."""

import hashlib
from datetime import datetime
from html import escape
from html.parser import HTMLParser
from typing import Any
from uuid import uuid4

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from sqlalchemy import BigInteger, CheckConstraint, DateTime, Integer, String, Text, func, select
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from cu_clubs import legacy, media
from cu_clubs.models import Base, Outbox

PAGE_PREFIX = "screen:page:"


class UIScreen(Base):
    __tablename__ = "ui_screens"
    __table_args__ = (
        CheckConstraint("kind IN ('text','photo')", name="ck_ui_screen_kind"),
        CheckConstraint("page_index >= 0", name="ck_ui_screen_page_index"),
    )

    bot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    message_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(10))
    text: Mapped[str] = mapped_column(Text)
    pages: Mapped[list[str]] = mapped_column(JSONB)
    base_markup: Mapped[dict | None] = mapped_column(JSONB)
    page_index: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    page_token: Mapped[str] = mapped_column(String(32))
    photo_file_id: Mapped[str | None] = mapped_column(Text)
    banner_role: Mapped[str | None] = mapped_column(String(50))
    banner_cache: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _pages(value: str, *, photo: bool) -> list[str]:
    # Preserve short legacy HTML byte for byte. Long screens become escaped plain
    # text pages, so neither HTML tags nor UTF-16 surrogate pairs can be split.
    if _units(value) <= (1000 if photo else 3900):
        return [value]
    parser = _PlainText()
    parser.feed(value)
    parser.close()
    visible = "".join(parser.parts)
    # Telegram counts characters after parsing entities; link URLs are not visible text.
    if _units(visible) <= (1000 if photo else 3900):
        return [value]
    limit = 900 if photo else 3700
    pages, chunk, size = [], [], 0
    for char in visible:
        length = _units(char)
        if chunk and size + length > limit:
            pages.append(escape("".join(chunk)))
            chunk, size = [], 0
        chunk.append(char)
        size += length
    if chunk or not pages:
        pages.append(escape("".join(chunk)))
    return pages


def _markup(screen: UIScreen) -> InlineKeyboardMarkup | None:
    base = (
        InlineKeyboardMarkup.model_validate(screen.base_markup)
        if screen.base_markup is not None
        else None
    )
    if len(screen.pages) == 1:
        return base
    navigation = []
    for label, page in (("←", screen.page_index - 1), ("→", screen.page_index + 1)):
        if 0 <= page < len(screen.pages):
            navigation.append(
                InlineKeyboardButton(
                    text=label, callback_data=f"{PAGE_PREFIX}{screen.page_token}:{page}"
                )
            )
    return InlineKeyboardMarkup(
        inline_keyboard=[*(base.inline_keyboard if base else []), navigation]
    )


def _page_text(screen: UIScreen) -> str:
    value = screen.pages[screen.page_index]
    if len(screen.pages) > 1:
        value += f"\n\n{screen.page_index + 1}/{len(screen.pages)}"
    return value


def _own_callback_message(bot: Bot, chat_id: int, trigger: Any) -> Message | None:
    if not isinstance(trigger, CallbackQuery):
        return None
    message = trigger.message
    sender = getattr(message, "from_user", None)
    if sender and sender.id == bot.id and message.chat.id == chat_id:
        return message
    return None


def _original_banner(photo: Any, role: str | None) -> bool:
    if not role or not isinstance(photo, BufferedInputFile):
        return False
    try:
        return media.is_banner(photo, role) or photo.data == legacy.banner(role).data
    except ValueError:
        return False


class ScreenManager:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        bot: Bot,
        *,
        file_ids: dict[tuple[int, str], str] | None = None,
    ):
        self.sessions, self.bot = sessions, bot
        self.file_ids = file_ids if file_ids is not None else {}

    async def _lock(self, session: AsyncSession, chat_id: int) -> None:
        digest = hashlib.blake2b(
            f"cu-clubs:screen:{self.bot.id}:{chat_id}".encode(), digest_size=8
        ).digest()
        await session.execute(
            sql_text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": int.from_bytes(digest, "big", signed=True)},
        )

    async def _delete(self, chat_id: int, message_id: int) -> bool:
        try:
            await self.bot.delete_message(chat_id=chat_id, message_id=message_id)
            return True
        except TelegramAPIError:
            # Rendering has succeeded. Cleanup is best effort and must not roll
            # back the newly saved message pointer or cause another render.
            return False

    async def _cleanup(
        self, chat_id: int, current: int, old: int | None, trigger: Any, extra_ids=()
    ) -> set[int]:
        remove = {old} if old is not None and old != current else set()
        remove.update(message_id for message_id in extra_ids if message_id != current)
        callback_message = _own_callback_message(self.bot, chat_id, trigger)
        if callback_message is not None and callback_message.message_id != current:
            remove.add(callback_message.message_id)
        if isinstance(trigger, Message):
            sender = trigger.from_user
            if trigger.chat.id == chat_id and sender and sender.id != self.bot.id:
                if trigger.message_id != current:
                    remove.add(trigger.message_id)
        deleted = set()
        for message_id in remove:
            if await self._delete(chat_id, message_id):
                deleted.add(message_id)
        return deleted

    async def _render(self, screen: UIScreen, photo: Any, old_kind: str | None) -> Any:
        content, markup = _page_text(screen), _markup(screen)
        if screen.message_id and old_kind == screen.kind:
            try:
                if screen.kind == "photo":
                    return await self.bot.edit_message_media(
                        chat_id=screen.chat_id,
                        message_id=screen.message_id,
                        media=InputMediaPhoto(media=photo, caption=content, parse_mode="HTML"),
                        reply_markup=markup,
                    )
                return await self.bot.edit_message_text(
                    chat_id=screen.chat_id,
                    message_id=screen.message_id,
                    text=content,
                    parse_mode="HTML",
                    reply_markup=markup,
                )
            except TelegramBadRequest as exc:
                message = exc.message.lower()
                if "message is not modified" in message:
                    return None
                if not any(
                    reason in message
                    for reason in (
                        "message to edit not found",
                        "message can't be edited",
                        "message cannot be edited",
                        "message_id_invalid",
                    )
                ):
                    raise
                # Only a definite gone/uneditable message allows a replacement.
        if screen.kind == "photo":
            return await self.bot.send_photo(
                chat_id=screen.chat_id,
                photo=photo,
                caption=content,
                parse_mode="HTML",
                reply_markup=markup,
            )
        return await self.bot.send_message(
            chat_id=screen.chat_id, text=content, parse_mode="HTML", reply_markup=markup
        )

    async def _clear_document(self, screen: UIScreen) -> None:
        cache = dict(screen.banner_cache or {})
        document_id = cache.pop("ui_document_message_id", None)
        if document_id:
            try:
                await self.bot.delete_message(chat_id=screen.chat_id, message_id=document_id)
            except TelegramBadRequest as error:
                if "message to delete not found" not in str(error).lower():
                    raise
        screen.banner_cache = cache

    async def send_document(self, chat_id: int, document: Any) -> None:
        """Track this window's file durably and remove it on the next navigation."""
        async with self.sessions.begin() as session:
            await self._lock(session, chat_id)
            screen = await session.get(UIScreen, (self.bot.id, chat_id))
            if screen is None:
                raise ValueError("Show the window before attaching its document")
            await self._clear_document(screen)
            result = await self.bot.send_document(chat_id=chat_id, document=document)
            screen.banner_cache = {
                **(screen.banner_cache or {}),
                "ui_document_message_id": result.message_id,
            }

    async def show(
        self,
        chat_id: int,
        text: str,
        markup: InlineKeyboardMarkup | None = None,
        photo: Any = None,
        *,
        banner_role: str | None = None,
        trigger: Message | CallbackQuery | None = None,
        force_new: bool = False,
    ) -> int:
        async with self.sessions.begin() as session:
            await self._lock(session, chat_id)
            screen = await session.get(UIScreen, (self.bot.id, chat_id))
            if screen:
                await self._clear_document(screen)
            callback_message = _own_callback_message(self.bot, chat_id, trigger)
            previous_id = screen.message_id if screen else None
            previous_kind = screen.kind if screen else None
            previous_photo_id = screen.photo_file_id if screen else None
            if screen is None:
                if callback_message:
                    previous_id = callback_message.message_id
                    previous_kind = "photo" if callback_message.photo else "text"
                screen = UIScreen(
                    bot_id=self.bot.id,
                    chat_id=chat_id,
                    message_id=previous_id or 0,
                    banner_cache={},
                )
                session.add(screen)
            original = _original_banner(photo, banner_role)
            role = banner_role if original else None
            fingerprint = hashlib.sha256(photo.data).hexdigest() if role else None
            cache_key = (self.bot.id, f"{role}:{fingerprint}") if role else None
            source_photo = photo
            if role:
                stored_banner = screen.banner_cache.get(role)
                cached = (
                    stored_banner.get("file_id")
                    if isinstance(stored_banner, dict)
                    and stored_banner.get("sha256") == fingerprint
                    else None
                )
                photo = self.file_ids.get(cache_key) or cached or photo
            screen.kind = "photo" if photo is not None else "text"
            screen.text, screen.pages = text, _pages(text, photo=photo is not None)
            screen.base_markup = (
                markup.model_dump(mode="json", exclude_none=True) if markup else None
            )
            screen.page_index, screen.page_token = 0, uuid4().hex[:16]
            screen.banner_role = role
            screen.photo_file_id = photo if isinstance(photo, str) else None
            try:
                result = await self._render(screen, photo, None if force_new else previous_kind)
            except TelegramBadRequest as exc:
                if (
                    role
                    and photo is not source_photo
                    and any(
                        marker in exc.message.lower()
                        for marker in ("wrong file identifier", "file reference", "file_id")
                    )
                ):
                    self.file_ids.pop(cache_key, None)
                    screen.photo_file_id = None
                    result = await self._render(
                        screen, source_photo, None if force_new else previous_kind
                    )
                else:
                    raise
            if result is not None and not isinstance(result, bool):
                screen.message_id = result.message_id
                if getattr(result, "photo", None):
                    screen.photo_file_id = result.photo[-1].file_id
            if screen.kind == "photo" and result is None and screen.photo_file_id is None:
                screen.photo_file_id = previous_photo_id
            if screen.kind == "photo" and role and screen.photo_file_id:
                self.file_ids[cache_key] = screen.photo_file_id
                screen.banner_cache = {
                    **screen.banner_cache,
                    role: {"sha256": fingerprint, "file_id": screen.photo_file_id},
                }
            await session.flush()
            # Also retire pre-single-screen club notifications: their URL buttons
            # open /start without providing the original message as a callback.
            notifications = list(
                await session.scalars(
                    select(Outbox).where(
                        Outbox.kind == "club.created",
                        Outbox.recipient_telegram_id == chat_id,
                        Outbox.status == "sent",
                        Outbox.telegram_message_id.is_not(None),
                        Outbox.telegram_message_id != screen.message_id,
                        Outbox.payload["ui_retired"].as_boolean().is_not(True),
                    )
                )
            )
            deleted = await self._cleanup(
                chat_id,
                screen.message_id,
                previous_id,
                trigger,
                [item.telegram_message_id for item in notifications],
            )
            for item in notifications:
                if item.telegram_message_id in deleted:
                    item.payload = {**item.payload, "ui_retired": True}
            return screen.message_id

    async def handle_page(self, callback: CallbackQuery) -> bool:
        data = callback.data or ""
        if not data.startswith(PAGE_PREFIX):
            return False
        message = callback.message
        if message is None or _own_callback_message(self.bot, message.chat.id, callback) is None:
            return True
        try:
            token, index = data[len(PAGE_PREFIX) :].split(":", 1)
            index = int(index)
        except ValueError:
            return True
        async with self.sessions.begin() as session:
            await self._lock(session, message.chat.id)
            screen = await session.get(UIScreen, (self.bot.id, message.chat.id))
            if screen is None or screen.page_token != token or not 0 <= index < len(screen.pages):
                return True
            await self._clear_document(screen)
            old_id = screen.message_id
            screen.page_index = index
            photo = screen.photo_file_id
            if screen.kind == "photo" and photo is None and screen.banner_role:
                try:
                    photo = media.banner(screen.banner_role)
                except ValueError:
                    photo = legacy.banner(screen.banner_role)
            result = await self._render(screen, photo, screen.kind)
            if result is not None and not isinstance(result, bool):
                screen.message_id = result.message_id
                if getattr(result, "photo", None):
                    screen.photo_file_id = result.photo[-1].file_id
            await session.flush()
            await self._cleanup(screen.chat_id, screen.message_id, old_id, callback)
        return True
