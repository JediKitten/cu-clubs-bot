"""Active Telegram artwork and screen-to-banner classification."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any

from aiogram.types import BufferedInputFile
from PIL import Image

RESOURCE_DIR = Path(__file__).with_name("media") / "resources"
MANIFEST_PATH = RESOURCE_DIR / "manifest.json"

with MANIFEST_PATH.open(encoding="utf-8") as _manifest_file:
    _manifest = json.load(_manifest_file)

ROLES: frozenset[str] = frozenset(_manifest["assets"])

# Callback prefixes are deliberately ordered from the more specific role to
# the broader destination. The conversation state remains a fallback for
# text-entry steps and stale/invalid callbacks.
_CALLBACK_ROLES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("faq",), "faq"),
    (("digest", "dg:"), "digest"),
    (
        ("su:", "suans", "sudone", "suskip", "suexitpreview", "supending"),
        "survey_answer",
    ),
    (
        (
            "subuilder",
            "suadd",
            "suchange",
            "suclub",
            "suevent",
            "sudelete",
            "sumove",
            "supage",
            "supreview",
            "suq",
            "suremove",
            "surequired",
            "susave",
            "sustepback",
            "sutype",
        ),
        "survey_builder",
    ),
    (("atledger", "atexport"), "rosters"),
    (("attendance", "attend", "checkin", "manualatt", "at"), "attendance"),
    (("uc:", "evics:", "my_events", "myevents", "future:mine"), "my_events"),
    (("calendar", "cal:", "calweek:", "calclub:", "eventsweek:"), "calendar"),
    (("apilink", "apiintegration"), "api"),
    (("api", "apiclient", "apigrants"), "api_clients"),
    (("notif", "notification", "nt", "nta", "ntc", "ntq"), "notifications"),
    (("analytic", "stat", "an"), "analytics"),
    (("roster", "participants", "members"), "rosters"),
    (
        (
            "collab",
            "partner",
            "evpartner",
            "evinvite",
            "evwithdraw",
            "eventinvitations",
            "evreview",
        ),
        "collaboration",
    ),
    (
        (
            "tmuse",
            "evblank",
            "evfromtemplate",
            "eventnew",
            "eventcreate",
            "eventdraft",
            "eventwizard",
        ),
        "event_create",
    ),
    (("tm", "evtemplate", "eventtemplate", "evtemplates"), "templates"),
    (("clubteam", "team"), "club_team"),
    (
        ("clubsettings", "clubsetting", "editmenu", "edit:", "save:", "cancel:", "cb:"),
        "club_settings",
    ),
    (("eventedit", "eventchange", "eventschedule", "eventroom"), "event_create"),
    (("admin",), "admin"),
    (("auth", "category"), "auth"),
    (("profile", "auth"), "personal-account"),
    (("clubedit", "club_batch", "clubsetting", "clubteam"), "club_settings"),
    (("organizers", "org"), "club_team"),
    (("manage",), "club-owner"),
    (("club:", "clubs", "subscribe:"), "clubs"),
    (("event:", "events", "future:"), "events"),
    (("menu", "cancel"), "menu"),
)

_STATE_ROLES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("auth_", "category_", "identity_", "yandex_", "settings"), "auth"),
    (("digest", "dg_"), "digest"),
    (("survey_answer", "survey_respond"), "survey_answer"),
    (("survey_",), "survey_builder"),
    (("attendance_",), "attendance"),
    (("calendar_",), "calendar"),
    (("notification_",), "notifications"),
    (("analytics_",), "analytics"),
    (("api", "apiclient"), "api_clients"),
    (("roster",), "rosters"),
    (("collab", "event_partner"), "collaboration"),
    (("template", "event_template"), "templates"),
    (("admin_",), "admin"),
    (("club_team",), "club_team"),
    (("organizer_",), "club_team"),
    (("club_setting", "club_batch"), "club_settings"),
    (("club_profile", "club_edit"), "club_settings"),
    (("event_create", "event_edit", "event_schedule"), "event_create"),
    (("my_events", "user_calendar"), "my_events"),
    (("event_",), "events"),
)


def _starts_with(value: str, prefixes: tuple[str, ...]) -> bool:
    return value.startswith(prefixes)


def resolve_banner_role(
    *,
    conversation_state: str | None = None,
    callback_data: str | None = None,
    suggested_role: str | None = None,
) -> str | None:
    """Resolve active artwork from the action, current step, then screen hint."""
    callback = (callback_data or "").strip().lower()
    if callback:
        for prefixes, role in _CALLBACK_ROLES:
            if _starts_with(callback, prefixes):
                return role

    state = (conversation_state or "").strip().lower()
    if state:
        for prefixes, role in _STATE_ROLES:
            if _starts_with(state, prefixes):
                return role

    if suggested_role in ROLES:
        return suggested_role
    # A general navigation illustration keeps malformed callbacks and generic
    # error screens visual when they have no more specific context.
    return "menu"


def original_bytes(role: str) -> bytes:
    """Return unchanged source bytes from the active manifest."""
    asset = _manifest["assets"].get(role)
    if asset is None:
        raise ValueError(f"unknown banner role: {role}")
    path = RESOURCE_DIR / asset["file"]
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != asset["sha256"]:
        raise ValueError(f"banner checksum mismatch: {role}")
    return data


@lru_cache(maxsize=24)
def upload_bytes(role: str) -> bytes:
    """Return original bytes or a cached proportional Telegram-safe copy."""
    data = original_bytes(role)
    with Image.open(BytesIO(data)) as image:
        width, height = image.size
        if width + height <= 10_000 and len(data) <= 10_000_000:
            return data

        # Keep the full artwork and aspect ratio. This is a transport derivative,
        # never written back to the source asset or its manifest hash.
        for edge in (4000, 2560, 2000, 1600):
            candidate = image.copy()
            candidate.thumbnail((edge, edge), Image.Resampling.LANCZOS)
            output = BytesIO()
            candidate.save(output, format="PNG", optimize=True)
            resized = output.getvalue()
            if candidate.width + candidate.height <= 10_000 and len(resized) <= 10_000_000:
                return resized
    raise ValueError(f"banner cannot be encoded within Telegram limits: {role}")


def banner(role: str) -> BufferedInputFile:
    """Load a Telegram-safe upload object without changing the source artwork."""
    if role not in _manifest["assets"]:
        raise ValueError(f"unknown banner role: {role}")
    return BufferedInputFile(upload_bytes(role), filename=f"{role}.png")


def is_banner(photo: Any, role: str | None) -> bool:
    if not role or role not in ROLES or not isinstance(photo, BufferedInputFile):
        return False
    return photo.data == upload_bytes(role)
