import asyncio
import hashlib
import json
from importlib.resources import files
from io import BytesIO

import pytest
from PIL import Image

from cu_clubs import media


@pytest.mark.parametrize(
    ("callback", "state", "suggested", "expected"),
    [
        ("faq:context:levels:seed", "idle", "menu", "faq"),
        ("dg:preview", "idle", "club-owner", "digest"),
        ("suans:nonce:1", "idle", "events", "survey_answer"),
        ("subuilder:nonce", "idle", None, "survey_builder"),
        ("suadd:nonce", "idle", None, "survey_builder"),
        ("suevent:cinema", "idle", None, "survey_builder"),
        ("suexitpreview:nonce", "idle", None, "survey_answer"),
        ("ntc:nonce", "idle", "club-owner", "notifications"),
        ("apilink:uuid", "idle", None, "api"),
        ("apiclient:uuid", "idle", None, "api_clients"),
        ("tmpls:cinema", "idle", None, "templates"),
        ("tmg:template:0", "idle", None, "templates"),
        ("tmedit:template", "idle", None, "templates"),
        ("tmfield:template:field", "idle", None, "templates"),
        ("tmuse:token", "idle", None, "event_create"),
        ("uc:mine:history:0", "idle", "events", "my_events"),
        ("evics:cinema", "idle", None, "my_events"),
        ("atledger:cinema", "idle", "events", "rosters"),
        ("atexportall:cinema", "idle", None, "rosters"),
        ("evpartner:cinema", "idle", "club-owner", "collaboration"),
        ("anreport:cinema", "idle", "club-owner", "analytics"),
        ("club:cinema", "idle", "events", "clubs"),
        ("subscribe:cinema:1", "idle", None, "clubs"),
        ("cb:start:cinema", "idle", None, "club_settings"),
        ("club_batch_save:cinema", "idle", None, "club_settings"),
        ("orgset:cinema:12:010", "idle", None, "club_team"),
        ("event:new", "idle", None, "events"),
        ("eventnew:cinema", "idle", "club-owner", "event_create"),
        (None, "auth_code", "menu", "auth"),
        (None, "survey_answer", "events", "survey_answer"),
        (None, "club_profile_edit", "club-owner", "club_settings"),
        (None, "yandex_name", "auth", "auth"),
        ("unknown:callback", "notification_text", "menu", "notifications"),
        ("unknown:callback", "idle", "calendar", "calendar"),
    ],
)
def test_banner_role_resolution(callback, state, suggested, expected):
    assert (
        media.resolve_banner_role(
            callback_data=callback,
            conversation_state=state,
            suggested_role=suggested,
        )
        == expected
    )


def test_unknown_route_uses_general_menu_artwork():
    assert media.resolve_banner_role(callback_data="unknown", conversation_state="idle") == "menu"


def test_manifest_has_all_24_checked_assets():
    manifest = json.loads(media.MANIFEST_PATH.read_text(encoding="utf-8"))
    assert len(manifest["assets"]) == 24
    assert set(manifest["assets"]) == media.ROLES
    for role, asset in manifest["assets"].items():
        content = (media.RESOURCE_DIR / asset["file"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == asset["sha256"], role
        assert len(content) == asset["bytes"], role
        assert media.original_bytes(role) == content
        assert media.banner(role).data == media.upload_bytes(role)


def test_transport_copies_fit_telegram_limits_without_changing_sources():
    manifest = json.loads(media.MANIFEST_PATH.read_text(encoding="utf-8"))
    resized_roles = set()
    for role, asset in manifest["assets"].items():
        original = media.original_bytes(role)
        upload = media.upload_bytes(role)
        with Image.open(BytesIO(upload)) as image:
            width, height = image.size
            assert image.format == "PNG"
            assert width + height <= 10_000, role
        assert len(upload) <= 10_000_000, role
        assert hashlib.sha256(original).hexdigest() == asset["sha256"], role
        if upload != original:
            resized_roles.add(role)
    assert resized_roles == {
        "auth",
        "menu",
        "personal-account",
        "clubs",
        "club-owner",
        "events",
    }


def test_active_manifest_is_separate_from_pinned_legacy_catalog():
    legacy_manifest = json.loads(
        files("cu_clubs.legacy").joinpath("resources/manifest.json").read_text(encoding="utf-8")
    )
    assert set(legacy_manifest["banners"]) == {
        "auth",
        "menu",
        "personal-account",
        "clubs",
        "club-owner",
        "events",
    }
    assert len(media.ROLES) == 24


def test_unknown_role_is_rejected():
    with pytest.raises(ValueError, match="unknown banner role"):
        media.banner("not-a-role")


def test_outbox_event_invitation_uses_collaboration_artwork(monkeypatch):
    from cu_clubs import event_ui, events, worker

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *args):
            return None

    class EventService:
        def __init__(self, session):
            pass

        async def pending_invitations(self, recipient):
            return [{"invitation_id": "invite-1", "title": "Совместная встреча"}]

    captured = {}

    class ScreenManager:
        def __init__(self, sessions, bot):
            pass

        async def show(self, recipient, text, markup, photo, *, banner_role, force_new):
            captured.update(
                recipient=recipient,
                text=text,
                photo=photo,
                banner_role=banner_role,
            )
            return 77

    monkeypatch.setattr(events, "EventService", EventService)
    monkeypatch.setattr(event_ui, "event_text", lambda event: "Текст встречи")
    monkeypatch.setattr(worker, "ScreenManager", ScreenManager)

    result = asyncio.run(
        worker.deliver_domain_notice(
            lambda: SessionContext(),
            object(),
            {"kind": "event.invitation", "recipient": 19, "payload": {"invitation": "invite-1"}},
            None,
        )
    )
    assert result.message_id == 77
    assert captured["banner_role"] == "collaboration"
    assert captured["photo"].data == media.banner("collaboration").data
