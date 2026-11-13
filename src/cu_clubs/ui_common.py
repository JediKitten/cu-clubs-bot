"""Presentation values shared by Telegram flows; no network or database operations."""

from dataclasses import dataclass
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def keyboard(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
            for row in rows
        ]
    )


@dataclass
class Screen:
    text: str
    markup: Any = None
    photo: Any = None
    banner_role: str | None = None
