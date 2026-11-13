"""Pinned upstream copy and owner-supplied artwork are immutable UI references."""

import asyncio
import hashlib
import json
from importlib.resources import files
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import BufferedInputFile
from PIL import Image

from cu_clubs import legacy, telegram

PINNED_COMMIT = "3a09cc0039d86641827dcbc28e74dd024b821732"
RESOURCE_HASHES = {
    "ru.yml": "26093ed533d4463ebe8dd88a30c0c3cd91cc1094d4f953a8126f8cf2e09cec03",
    "telegram.yml": "353ef46dd9abaa810d236b217fd698154605c7145c6f8611b89fb2f6cc1298ec",
    "logo.png": "b20396fb1db553f7236b4aeb800e665cf6b8ba8b59778a68a4b701cd36103b8b",
    "mail.html": "c75b06776ce5e146e43196a6c8a596ca370fc0a100a9dcc991757c8edc338d33",
    "ru.json": "d8b797d3f1e62fe4b26d920ee5be0958c8ab846ebe4adfcac75a2ff9a6f74b59",
}
BANNER_HASHES = {
    "auth": "b5d201d4523b4005acf333728d32306b9c75bffc3e43325a04716128e3aa559f",
    "events": "8e7379b09dd27cb277ea17e98cd910a4abdf6cdb04f51d3f3c0c8ff47b7d2b15",
    "personal-account": "a872512e233d5f1843f0844f3c0f29998e1d1e397726f6d4c47ee2c2308b10c8",
    "menu": "e51f5a259e71022a15405750eddd250887f443297c3f100cb03a7c396adef1c5",
    "club-owner": "4acc4d1c6a266e3966a858074375cb64bd859790d7fc4f662a65f4b345e167c1",
    "clubs": "e0aa20eb3f1028fdecccab8eaf56927e704eaadc2c655d3d8d4d443403cd822d",
}


def resources():
    return files(legacy).joinpath("resources")


@pytest.mark.parametrize("filename,expected", RESOURCE_HASHES.items())
def test_pinned_reference_bytes_and_manifest_agree(filename, expected):
    manifest = json.loads(resources().joinpath("manifest.json").read_text(encoding="utf-8"))
    assert manifest["upstream"] == "https://github.com/nlypage/cu-clubs-bot"
    assert manifest["commit"] == PINNED_COMMIT
    assert manifest["message_count"] == 214
    assert manifest["resources"][filename]["sha256"] == expected
    assert hashlib.sha256(resources().joinpath(filename).read_bytes()).hexdigest() == expected


@pytest.mark.parametrize("role,expected", BANNER_HASHES.items())
def test_six_original_banners_are_packaged_at_original_size(role, expected):
    manifest = json.loads(resources().joinpath("manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["banners"]) == set(BANNER_HASHES)
    packaged_banners = {
        name
        for name, entry in manifest["resources"].items()
        if entry.get("source") in {"user_original", "user_replacement"}
    }
    assert packaged_banners == {f"{name}.png" for name in BANNER_HASHES}
    asset = legacy.banner(role)
    assert isinstance(asset, BufferedInputFile)
    assert asset.filename == f"{role}.png"
    original = resources().joinpath(asset.filename).read_bytes()
    assert hashlib.sha256(original).hexdigest() == expected
    upload_file = manifest["banners"][role].get("upload_file", asset.filename)
    assert asset.data == resources().joinpath(upload_file).read_bytes()
    assert manifest["resources"][asset.filename]["sha256"] == expected
    origin = "user_replacement" if role == "menu" else "user_original"
    assert manifest["resources"][asset.filename]["source"] == origin
    metadata = manifest["banners"][role]
    if "dimensions" in metadata:
        assert metadata["dimensions"] == ([5120, 5120] if role == "menu" else [2560, 2560])
    assert {
        key: metadata[key]
        for key in (
            "source_config_key",
            "status",
            "file",
            "origin",
            "sha256",
        )
    } == {
        "source_config_key": f"bot.banner.{role}",
        "status": "available",
        "file": asset.filename,
        "origin": origin,
        "sha256": expected,
    }
    with Image.open(BytesIO(asset.data)) as image:
        assert image.format == "PNG"
        assert image.size == (2560, 2560)
        image.verify()


def test_unknown_banner_role_is_rejected():
    with pytest.raises(ValueError, match="Unknown original banner role"):
        legacy.banner("invented")


@pytest.mark.parametrize(
    "key,expected",
    {
        "main_menu_text": "<b>Главное меню</b>",
        "cu_clubs": "Клубы ЦУ",
        "events": "Мероприятия",
        "personal_account": "Личный кабинет",
        "my_events": "Мои мероприятия",
        "my_clubs": "Мои клубы",
        "admin_menu": "Админ-меню",
        "cu_clubs_list": "Список клубов ЦУ",
    }.items(),
)
def test_catalog_has_exact_original_shared_labels(key, expected):
    assert len(legacy.catalog()) == 214
    assert legacy.text(key) == expected


@pytest.mark.parametrize(
    "count,expected",
    [
        (0, "<b>У вас нет ни одного клуба</b>"),
        (1, "<b>Список ваших клубов</b>\n\n<i>Всего:</i> <b>1</b>"),
        (12, "<b>Список ваших клубов</b>\n\n<i>Всего:</i> <b>12</b>"),
    ],
)
def test_owned_club_count_keeps_original_text(count, expected):
    assert legacy.owned_clubs_text(count) == expected


def test_club_card_preserves_original_block_and_appends_new_fields():
    club = {
        "name": "Кино & <клуб>",
        "description": "Фильмы\nи <b>встречи</b>",
        "channel": "https://t.me/example?a=1&b=2",
        "level": "Seed",
        "public_team": [{"name": "<Кир>", "role": "Кино & программа"}],
    }
    original_block = (
        "Клуб: <b>Кино &amp; &lt;клуб&gt;</b>\n\n<b>Описание:</b>\n"
        "<blockquote>Фильмы\nи &lt;b&gt;встречи&lt;/b&gt;</blockquote>\n\n"
        "<b>Ссылка на чат/канал клуба:</b>\nhttps://t.me/example?a=1&amp;b=2"
    )
    assert legacy.club_base_text(club) == original_block
    assert telegram.card_text(club) == (
        original_block + "\n\n<b>Уровень:</b> Seed\n\n<b>Команда</b>\n"
        "• &lt;Кир&gt; — Кино &amp; программа"
        "\n\nБлижайшие встречи — в разделе «Мероприятия»."
    )


def test_owner_club_card_keeps_exact_original_text_and_real_owner_block():
    assert legacy.owner_club_text(
        {"name": "Киноклуб", "description": "Смотрим фильмы", "channel": "@cinema"},
        [{"full_name": "Кир", "username": "kir"}, {"full_name": "Аня", "username": "anna"}],
    ) == (
        "Клуб: <b>Киноклуб</b>\n\n<u>Организаторы:</u>\n"
        "- <b>Кир</b> (@kir)\n- <b>Аня</b> (@anna)\n"
        "\n<b>Описание:</b>\n<blockquote>Смотрим фильмы</blockquote>"
        "\n\n<b>Ссылка на чат/канал клуба:</b>\n@cinema"
    )
    assert legacy.owner_club_text({"name": "Киноклуб"}, []) == (
        "Клуб: <b>Киноклуб</b>\n\n<u>Организаторы:</u>\n"
        "<i>- Отсутствуют</i>\n\n<b>Описание:</b>\n"
        "<blockquote><i>Не указано</i></blockquote>"
        "\n\n<b>Ссылка на чат/канал клуба:</b>\nНе указано"
    )


def test_owner_club_renderer_escapes_html_and_keeps_go_looking_input_literal():
    literal = "{{html .Club.Name}} <Кино> &"
    escaped = "{{html .Club.Name}} &lt;Кино&gt; &amp;"
    rendered = legacy.owner_club_text(
        {"name": literal, "description": literal, "channel": literal},
        [{"full_name": literal, "username": "{{.Username}}<user>&"}],
    )
    assert rendered.count(escaped) == 4
    assert "(@{{.Username}}&lt;user&gt;&amp;)" in rendered


def test_empty_club_fields_keep_original_fallbacks():
    assert legacy.club_base_text({"name": "Киноклуб"}) == (
        "Клуб: <b>Киноклуб</b>\n\n<b>Описание:</b>\n"
        "<blockquote><i>Не указано</i></blockquote>\n\n"
        "<b>Ссылка на чат/канал клуба:</b>\nНе указана"
    )


def test_go_looking_user_input_remains_literal_and_html_is_escaped():
    literal = '{{html .Club.Name}} {{.Name}} {{if .Club.Link}}<b>&"'
    escaped = "{{html .Club.Name}} {{.Name}} {{if .Club.Link}}&lt;b&gt;&amp;&quot;"
    club = {"name": literal, "description": literal, "channel": literal}
    rendered = legacy.club_base_text(club)
    assert rendered.count(escaped) == 3
    assert legacy.personal_account_text(literal) == (
        f"<i>Добро пожаловать {escaped}, ваша роль <b>не определена</b></i>"
    )


def test_club_creation_keeps_original_copy_and_escapes_literal_input():
    assert legacy.club_created_text("Киноклуб") == "Клуб <b>Киноклуб</b> успешно создан!"
    assert legacy.club_created_text("{{html .Name}} <Кино> &") == (
        "Клуб <b>{{html .Name}} &lt;Кино&gt; &amp;</b> успешно создан!"
    )


def test_raw_go_templates_require_explicit_renderer():
    with pytest.raises(ValueError, match="explicit reviewed renderer"):
        legacy.text("cu_club_text")
    with pytest.raises(ValueError, match="explicit reviewed renderer"):
        legacy.text("personal_account_text")


def test_reviewed_renderer_rejects_new_unsupported_go_expression(monkeypatch):
    changed = {**legacy.catalog(), "cu_club_text": legacy.catalog()["cu_club_text"] + "{{.New}}"}
    monkeypatch.setattr(legacy, "catalog", lambda: changed)
    with pytest.raises(ValueError, match="compatibility review is required"):
        legacy.club_base_text({"name": "Киноклуб"})


@pytest.mark.parametrize(
    "role,label",
    [
        (None, "не определена"),
        ("unverified", "не определена"),
        ("student", "студент"),
        ("grant_user", "абитуриент"),
        ("external_user", "внешний пользователь"),
    ],
)
def test_personal_account_roles_are_explicit(role, label):
    assert legacy.personal_account_text("<Actor>", role) == (
        f"<i>Добро пожаловать &lt;Actor&gt;, ваша роль <b>{label}</b></i>"
    )


@pytest.mark.parametrize("units,photo_caption", [(1000, True), (1002, False)])
def test_photo_caption_limit_counts_utf16_units(units, photo_caption):
    async def scenario():
        bot = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())
        caption = "😀" * (units // 2)
        markup = telegram.keyboard([("В меню", "menu")])
        photo = legacy.banner("clubs")
        await telegram._send_screen(bot, 101, caption, markup, photo)
        bot.send_photo.assert_awaited_once()
        if photo_caption:
            bot.send_photo.assert_awaited_once_with(
                101, photo, caption=caption, parse_mode="HTML", reply_markup=markup
            )
            bot.send_message.assert_not_awaited()
        else:
            bot.send_photo.assert_awaited_once_with(101, photo)
            bot.send_message.assert_awaited_once_with(
                101, caption, parse_mode="HTML", reply_markup=markup
            )

    asyncio.run(scenario())
