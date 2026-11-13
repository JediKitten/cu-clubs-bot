"""Pinned original UI copy. New features extend the existing text instead of rewriting it."""

import json
import re
from functools import lru_cache
from html import escape
from importlib.resources import files

from aiogram.types import BufferedInputFile


@lru_cache(maxsize=1)
def catalog() -> dict[str, str]:
    return json.loads(files(__package__).joinpath("resources/ru.json").read_text(encoding="utf-8"))


def text(key: str) -> str:
    value = catalog()[key]
    if "{{" in value:
        raise ValueError(f"Legacy template {key} needs an explicit reviewed renderer")
    return value


def owned_clubs_text(count: int) -> str:
    if count == 0:
        return text("no_clubs")
    return catalog()["my_clubs_list"].replace("{{.}}", str(count))


def _render(key: str, replacements: dict[str, str]) -> str:
    original = catalog()[key]
    pattern = re.compile("|".join(re.escape(token) for token in replacements))
    if "{{" in pattern.sub("", original):
        raise ValueError(f"Legacy template {key} changed; compatibility review is required")
    # Substitute once: text supplied by users is data, even when it contains Go-like braces.
    return pattern.sub(lambda match: replacements[match.group(0)], original)


def club_base_text(club: dict) -> str:
    """Render the exact old club block; approved new fields are appended by the adapter."""
    replacements = {
        "{{html .Club.Name}}": escape(club["name"]),
        "{{if .Club.Description}}{{html .Club.Description}}{{else}}<i>Не указано</i>{{end}}": (
            escape(club["description"]) if club.get("description") else "<i>Не указано</i>"
        ),
        "{{if .Club.Link}}{{html .Club.Link}}{{else}}Не указана{{end}}": (
            escape(club["channel"]) if club.get("channel") else "Не указана"
        ),
    }
    return _render("cu_club_text", replacements)


def personal_account_text(full_name: str, role: str | None = None) -> str:
    role_expression = (
        '{{if eq .Role "student"}}студент{{else if eq .Role "grant_user"}}абитуриент'
        '{{else if eq .Role "external_user"}}внешний пользователь{{else}}не определена{{end}}'
    )
    role_label = {
        "student": "студент",
        "grant_user": "абитуриент",
        "external_user": "внешний пользователь",
    }.get(role, "не определена")
    return _render(
        "personal_account_text",
        {
            "{{.Name}}": escape(full_name),
            role_expression: role_label,
        },
    )


def club_created_text(name: str) -> str:
    return _render("club_created", {"{{html .Name}}": escape(name)})


def owner_club_text(club: dict, owners: list[dict]) -> str:
    owners_expression = (
        "{{if .Owners}}{{range .Owners}}- <b>{{html .FIO}}</b> (@{{.Username}})"
        '{{"\\n"}}{{end}}{{else}}<i>- Отсутствуют</i>{{"\\n"}}{{end}}'
    )
    people = (
        "".join(
            f"- <b>{escape(owner['full_name'])}</b> (@{escape(owner.get('username') or '')})\n"
            for owner in owners
        )
        or "<i>- Отсутствуют</i>\n"
    )
    return _render(
        "club_owner_club_menu_text",
        {
            "{{html .Club.Name}}": escape(club["name"]),
            owners_expression: people,
            "{{if .Club.Description}}{{html .Club.Description}}{{else}}<i>Не указано</i>{{end}}": (
                escape(club["description"]) if club.get("description") else "<i>Не указано</i>"
            ),
            "{{if .Club.Link}}{{html .Club.Link}}{{else}}Не указано{{end}}": (
                escape(club["channel"]) if club.get("channel") else "Не указано"
            ),
        },
    )


@lru_cache(maxsize=6)
def _banner_bytes(role: str) -> bytes:
    if role not in {"auth", "menu", "personal-account", "clubs", "club-owner", "events"}:
        raise ValueError("Unknown original banner role")
    resources = files(__package__).joinpath("resources")
    manifest = json.loads(resources.joinpath("manifest.json").read_text())
    filename = manifest["banners"][role].get("upload_file", f"{role}.png")
    return resources.joinpath(filename).read_bytes()


def banner(role: str) -> BufferedInputFile:
    return BufferedInputFile(_banner_bytes(role), filename=f"{role}.png")
