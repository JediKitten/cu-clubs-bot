"""Source attribution, current bot behavior and bounded contextual help."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_events import STUDENT, seed
from test_events import sessions as sessions
from test_expanded_dispatch import settings, shown, transport

from cu_clubs import faq, faq_ui
from cu_clubs.faq_ui import handle_faq
from cu_clubs.identity import IdentityService
from cu_clubs.models import Conversation
from cu_clubs.services import ValidationError


def test_faq_resource_versions_bot_instructions_and_never_fabricates_handbook_rules():
    resource = faq.content()
    assert resource["version"] and resource["bot"]["verified_at"] == "2026-11-16"
    assert resource["bot"]["sources"] and resource["handbook_url"] == faq.HANDBOOK_URL
    assert resource["handbook"]["verified_at"] == "2026-11-16"
    assert resource["handbook"]["source"] == faq.HANDBOOK_URL
    assert {level["name"] for level in faq.handbook_entries("levels")} == {
        "Concept",
        "Pre-Seed",
        "Seed",
        "Pre-IPO",
        "IPO",
    }
    seed_level = next(
        item["answer"] for item in faq.handbook_entries("levels") if item["name"] == "Seed"
    )
    assert "4,0" in seed_level and "X" in seed_level and "трёх месяцев" in seed_level
    assert "бюджет" not in seed_level
    declared = next(
        item for item in faq.bot_entries(self_registration=True) if item["id"] == "registration"
    )
    verified = next(
        item for item in faq.bot_entries(self_registration=False) if item["id"] == "registration"
    )
    assert "сам ввод логина не подтверждает" in declared["answer"]
    assert "подтверждённая студенческая почта" in verified["answer"]
    assert "Гостевой вход планируется" not in str(resource)
    assert "не подключён" not in str(resource["bot"])


def test_every_faq_screen_links_to_handbook_and_context_does_not_expand_scope():
    async def scenario():
        session = SimpleNamespace(info={"test_self_registration_enabled": True})
        state = SimpleNamespace(state="idle", payload={})

        async def screen(route):
            result = await handle_faq(session, STUDENT, state, route)
            assert faq.HANDBOOK_URL in result.text
            assert any(
                button.url == faq.HANDBOOK_URL
                for row in result.markup.inline_keyboard
                for button in row
            )
            assert all(
                len(button.callback_data.encode()) <= 64
                for row in result.markup.inline_keyboard
                for button in row
                if button.callback_data
            )
            return result

        main = await screen("faq")
        callbacks = [button.callback_data for row in main.markup.inline_keyboard for button in row]
        assert all(f"faq:section:{section}" in callbacks for section in ("clubs", "levels", "bot"))
        bot = await screen("faq:section:bot")
        assert "Как пользоваться ботом" in bot.text
        article = await screen("faq:entry:bot:1")
        assert "сам ввод логина" in article.text
        assert any(
            button.callback_data == "faq:section:bot"
            for row in article.markup.inline_keyboard
            for button in row
        )
        contextual = await screen("faq:context:bot:profile")
        assert any(
            button.callback_data == "profile"
            for row in contextual.markup.inline_keyboard
            for button in row
        )
        assert (
            "Как пользоваться ботом" in contextual.text and "Уровни клубов" not in contextual.text
        )
        article = await screen("faq:entry:bot:1")
        assert any(
            button.callback_data == "faq:section:bot"
            for row in article.markup.inline_keyboard
            for button in row
        )
        with pytest.raises(ValidationError):
            await screen("faq:section:clubs")
        with pytest.raises(ValidationError):
            await screen("faq:entry:levels:0")
        await screen("faq")
        await screen("faq:section:clubs")
        await screen("faq:section:levels")
        for invalid in (
            "faq:entry:bot:-1",
            "faq:entry:bot:500",
            "faq:context:bot:admin",
            "faq:section:bad",
        ):
            with pytest.raises(ValidationError):
                await screen(invalid)

    asyncio.run(scenario())


def test_real_dispatch_exposes_full_faq_and_scoped_club_level_with_returns(sessions):
    async def scenario():
        await seed(sessions)
        sessions.configure(info={"test_self_registration_enabled": True})
        async with sessions.begin() as session:
            await IdentityService(session, "", mail_available=False).declare_category(
                STUDENT, "student"
            )
        config = settings()
        config.test_self_registration_enabled = True
        bot, feed, action = transport(sessions, config)

        def has_handbook():
            assert faq.HANDBOOK_URL in shown(bot.sent[-1])
            assert any(
                button.url == faq.HANDBOOK_URL
                for row in bot.sent[-1].reply_markup.inline_keyboard
                for button in row
            )

        try:
            await feed(STUDENT, data="menu")
            await feed(STUDENT, data=action("FAQ"))
            has_handbook()
            await feed(STUDENT, data=action("Про клубы"))
            has_handbook()
            await feed(STUDENT, data=action("Как открыть клуб?"))
            assert "план развития" in shown(bot.sent[-1])
            assert any(
                button.url == "https://polls.tbank.ru/s/cm0ntgd2b01rd0duehs1eb4b8"
                for row in bot.sent[-1].reply_markup.inline_keyboard
                for button in row
            )
            has_handbook()
            await feed(STUDENT, data=action("Назад"))
            await feed(STUDENT, data=action("Назад"))
            await feed(STUDENT, data=action("Уровни клубов"))
            assert {
                button.text for row in bot.sent[-1].reply_markup.inline_keyboard for button in row
            } >= {"Concept", "Pre-Seed", "Seed", "Pre-IPO", "IPO"}
            await feed(STUDENT, data=action("IPO"))
            assert "двух лет" in shown(bot.sent[-1]) and "4,5" in shown(bot.sent[-1])
            has_handbook()
            await feed(STUDENT, data="club:cinema")
            assert action("Что даёт уровень?") == "faq:context:levels:cinema"
            await feed(STUDENT, data=action("Что даёт уровень?"))
            text = shown(bot.sent[-1])
            assert "Seed" in text and "4,0" in text and "Pre-IPO" not in text
            assert action("Назад") == "club:cinema"
            has_handbook()
            await feed(STUDENT, data=action("Назад"))
            assert "Киноклуб" in shown(bot.sent[-1])
            await feed(STUDENT, data="faq:context:bot:profile")
            await feed(STUDENT, data=action("Как записаться?"))
            assert "сам ввод логина" in shown(bot.sent[-1])
            has_handbook()
            assert action("Назад") == "faq:section:bot"
            await feed(STUDENT, data=action("Назад"))
            assert action("Назад") == "profile"
            await feed(STUDENT, data=action("Назад"))
            assert "Логин в Тайме" in shown(bot.sent[-1])
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_contextual_level_shows_only_assigned_level_and_returns_to_club(sessions, monkeypatch):
    # Synthetic handbook data verifies routing without claiming real source content.
    resource = deepcopy(faq.content())
    resource["handbook"] = {
        "verified_at": "2026-11-16",
        "clubs": [],
        "levels": [
            {"name": "Seed", "answer": "SYNTHETIC_SEED"},
            {"name": "IPO", "answer": "SYNTHETIC_IPO"},
        ],
    }
    monkeypatch.setattr(faq, "content", lambda: resource)
    monkeypatch.setattr(faq_ui, "content", lambda: resource)

    async def scenario():
        await seed(sessions)
        async with sessions.begin() as session:
            state = Conversation(
                telegram_id=STUDENT, state="idle", payload={"club_origin": "cinema"}
            )
            session.add(state)
            contextual = await handle_faq(session, STUDENT, state, "faq:context:levels:cinema")
            assert "SYNTHETIC_SEED" in contextual.text and "SYNTHETIC_IPO" not in contextual.text
            assert any(
                button.callback_data == "club:cinema"
                for row in contextual.markup.inline_keyboard
                for button in row
            )
            assert all(
                not (button.callback_data or "").startswith("faq:")
                for row in contextual.markup.inline_keyboard
                for button in row
            )
            with pytest.raises(ValidationError):
                await handle_faq(session, STUDENT, state, "faq:section:bot")
            with pytest.raises(ValidationError):
                await handle_faq(session, STUDENT, state, "faq:section:levels")
            with pytest.raises(ValidationError):
                await handle_faq(session, STUDENT, state, "faq:entry:levels:1")
            state.payload = {"club_origin": "cinema"}
            old = await handle_faq(session, STUDENT, state, "faq:levels")
            assert "SYNTHETIC_SEED" in old.text and "SYNTHETIC_IPO" not in old.text
            root = await handle_faq(session, STUDENT, state, "faq")
            assert "FAQ" in root.text
            levels = await handle_faq(session, STUDENT, state, "faq:section:levels")
            assert (
                len(
                    [
                        button
                        for row in levels.markup.inline_keyboard
                        for button in row
                        if (button.callback_data or "").startswith("faq:entry:")
                    ]
                )
                == 2
            )
            article = await handle_faq(session, STUDENT, state, "faq:entry:levels:1")
            assert "SYNTHETIC_IPO" in article.text and "SYNTHETIC_SEED" not in article.text

    asyncio.run(scenario())
