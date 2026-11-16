"""PostgreSQL and real dispatcher checks for atomic, retained profile batches."""

import asyncio

import pytest
from sqlalchemy import func, select
from test_events import ADMIN, LEADER, OTHER, seed
from test_events import sessions as sessions
from test_expanded_dispatch import settings, shown, transport

from cu_clubs.club_edit_ui import DRAFT_KEY
from cu_clubs.models import Audit, Conversation, Membership
from cu_clubs.services import ClubService, PermissionDenied, ValidationError
from cu_clubs.telegram import FIELDS


async def club(factory):
    async with factory() as session:
        return await ClubService(session).get_club("cinema")


def test_batch_rolls_back_all_prior_fields_and_audits_inside_callers_transaction(sessions):
    async def scenario():
        await seed(sessions)
        original = await club(sessions)
        async with sessions.begin() as session:
            service = ClubService(session)
            initial_audits = await session.scalar(select(func.count()).select_from(Audit))
            initial_memberships = await session.scalar(select(func.count()).select_from(Membership))
            with pytest.raises(ValidationError):
                await service.update_profile_fields(
                    LEADER,
                    "cinema",
                    {
                        "name": "Подготовленное имя",
                        "public_team": [{"name": "Публичный участник", "role": "Ведущий"}],
                        "channel": "not-a-url",
                    },
                    expected_version=original["version"],
                )
            assert await service.get_club("cinema") == original
            assert await session.scalar(select(func.count()).select_from(Audit)) == initial_audits
            assert (
                await session.scalar(select(func.count()).select_from(Membership))
                == initial_memberships
            )
            session.add(
                Conversation(
                    telegram_id=LEADER, state="club_batch_preview", payload={"retained": "draft"}
                )
            )
        assert await club(sessions) == original
        async with sessions() as session:
            assert (await session.get(Conversation, LEADER)).payload == {"retained": "draft"}

    asyncio.run(scenario())


def test_competing_batches_with_same_version_have_one_winner(sessions):
    async def scenario():
        await seed(sessions)
        original = await club(sessions)

        async def apply(actor, name):
            try:
                async with sessions.begin() as session:
                    await ClubService(session).update_profile_fields(
                        actor,
                        "cinema",
                        {"name": name, "direction": "Health Hub"},
                        expected_version=original["version"],
                    )
                return "saved"
            except ValidationError:
                return "conflict"

        results = await asyncio.gather(
            apply(LEADER, "Первое новое имя"), apply(ADMIN, "Второе новое имя")
        )
        assert sorted(results) == ["conflict", "saved"]
        current = await club(sessions)
        assert current["version"] == original["version"] + 2
        assert current["name"] in {"Первое новое имя", "Второе новое имя"}

    asyncio.run(scenario())


def test_real_dispatch_retains_input_on_back_and_restart_and_saves_only_whole_batch(sessions):
    async def scenario():
        await seed(sessions)
        original = await club(sessions)
        bot, feed, action = transport(sessions, settings())
        try:
            await feed(LEADER, data="editmenu:cinema")
            await feed(LEADER, data=action("Изменить несколько полей"))
            await feed(LEADER, data=action(FIELDS["name"]))
            await feed(LEADER, text="X")
            assert "Твой ввод: X" in shown(bot.sent[-1])
            await feed(LEADER, data=action("Назад"))
            await feed(LEADER, data=action(FIELDS["name"]), restart=True)
            assert "Твой ввод: X" in shown(bot.sent[-1])
            await feed(LEADER, text="Новая карточка клуба")
            assert "Новая карточка клуба" in shown(bot.sent[-1])
            assert await club(sessions) == original
            await feed(LEADER, data=action("Изменить ещё"))
            await feed(LEADER, data=action(FIELDS["description"]))
            await feed(LEADER, text="Новое подробное описание клуба")
            assert "Новая карточка клуба" in shown(bot.sent[-1]) and "Новое подробное" in shown(
                bot.sent[-1]
            )
            assert await club(sessions) == original
            save = action("Сохранить изменения")
            async with sessions() as session:
                draft = (await session.get(Conversation, LEADER)).payload[DRAFT_KEY]
                assert draft["changes"] == {
                    "name": "Новая карточка клуба",
                    "description": "Новое подробное описание клуба",
                }
            await feed(LEADER, data=save, restart=True)
            assert "Изменения сохранены" in shown(bot.sent[-1])
            updated = await club(sessions)
            assert (
                updated["name"] == "Новая карточка клуба"
                and updated["description"] == "Новое подробное описание клуба"
            )
            assert updated["version"] == original["version"] + 2
            await feed(LEADER, data=save)
            assert "устарела" in shown(bot.sent[-1])
            assert await club(sessions) == updated
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_conflict_retains_values_and_requires_fresh_preview_confirmation(sessions):
    async def scenario():
        await seed(sessions)
        original = await club(sessions)
        bot, feed, action = transport(sessions, settings())
        try:
            await feed(LEADER, data="cb:start:cinema")
            await feed(LEADER, data=action(FIELDS["name"]))
            await feed(LEADER, text="Имя из сохранённого черновика")
            old_save = action("Сохранить изменения")
            async with sessions.begin() as session:
                await ClubService(session).update_club(
                    ADMIN,
                    "cinema",
                    "description",
                    "Правка другого редактора",
                    expected_version=original["version"],
                )
            await feed(LEADER, data=old_save)
            assert "Карточка изменилась" in shown(bot.sent[-1])
            assert (await club(sessions))["name"] == original["name"]
            async with sessions() as session:
                assert (await session.get(Conversation, LEADER)).payload[DRAFT_KEY]["changes"][
                    "name"
                ] == "Имя из сохранённого черновика"
            await feed(LEADER, data=action("Проверить с актуальной карточкой"))
            assert "Правка другого редактора" in shown(
                bot.sent[-1]
            ) and "Имя из сохранённого черновика" in shown(bot.sent[-1])
            new_save = action("Сохранить изменения")
            assert old_save != new_save
            await feed(LEADER, data=old_save)
            assert "устарела" in shown(bot.sent[-1])
            await feed(LEADER, data=new_save)
            current = await club(sessions)
            assert (
                current["name"] == "Имя из сохранённого черновика"
                and current["description"] == "Правка другого редактора"
            )
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_cancel_discards_pending_batch_and_revoked_permission_cannot_save(sessions):
    async def scenario():
        await seed(sessions)
        original = await club(sessions)
        async with sessions.begin() as session:
            await ClubService(session).set_organizer(LEADER, "cinema", OTHER)
        bot, feed, action = transport(sessions, settings())
        try:
            await feed(OTHER, data="cb:start:cinema")
            await feed(OTHER, data=action(FIELDS["direction"]))
            await feed(OTHER, data=action("Health Hub"))
            await feed(OTHER, data=action("Отменить"))
            assert await club(sessions) == original
            await feed(OTHER, data="cb:start:cinema")
            await feed(OTHER, data=action(FIELDS["name"]))
            await feed(OTHER, text="Правка организатора")
            save = action("Сохранить изменения")
            async with sessions.begin() as session:
                await ClubService(session).set_organizer(
                    LEADER, "cinema", OTHER, edit_profile=False
                )
            await feed(OTHER, data=save)
            assert "Нет права" in shown(bot.sent[-1])
            assert await club(sessions) == original
            async with sessions() as session:
                assert (await session.get(Conversation, OTHER)).payload[DRAFT_KEY]["changes"][
                    "name"
                ] == "Правка организатора"
            async with sessions.begin() as session:
                with pytest.raises(PermissionDenied):
                    await ClubService(session).update_profile_fields(
                        OTHER,
                        "cinema",
                        {"name": "Не должно сохраниться"},
                        expected_version=original["version"],
                    )
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_failed_shared_validation_keeps_entire_ui_draft_and_applies_no_field(sessions):
    async def scenario():
        await seed(sessions)
        original = await club(sessions)
        bot, feed, action = transport(sessions, settings())
        try:
            await feed(LEADER, data="cb:start:cinema")
            await feed(LEADER, data=action(FIELDS["name"]))
            await feed(LEADER, text="Имя из общего черновика")
            await feed(LEADER, data=action("Изменить ещё"))
            await feed(LEADER, data=action(FIELDS["channel"]))
            await feed(LEADER, text="https://")
            await feed(LEADER, data=action("Сохранить изменения"))
            assert "Нужна полная ссылка" in shown(bot.sent[-1])
            assert await club(sessions) == original
            async with sessions() as session:
                draft = (await session.get(Conversation, LEADER)).payload[DRAFT_KEY]
                assert draft["changes"] == {
                    "name": "Имя из общего черновика",
                    "channel": "https://",
                }
            await feed(LEADER, data=action("Назад"))
            assert "Твой ввод: https://" in shown(bot.sent[-1])
            await feed(LEADER, text="@public_club")
            await feed(LEADER, data=action("Сохранить изменения"))
            current = await club(sessions)
            assert current["name"] == "Имя из общего черновика"
            assert current["channel"] == "https://t.me/public_club"
        finally:
            await bot.session.close()

    asyncio.run(scenario())


def test_back_after_invalid_replacement_cannot_silently_save_previous_value(sessions):
    async def scenario():
        await seed(sessions)
        original = await club(sessions)
        bot, feed, action = transport(sessions, settings())
        try:
            await feed(LEADER, data="cb:start:cinema")
            await feed(LEADER, data=action(FIELDS["name"]))
            await feed(LEADER, text="Первое подготовленное имя")
            await feed(LEADER, data=action("Назад"))
            await feed(LEADER, text="X")
            await feed(LEADER, data=action("Назад"))
            await feed(LEADER, data=action("Общий предпросмотр"))
            assert "Нужно исправить ввод" in shown(bot.sent[-1])
            assert all(
                button.text != "Сохранить изменения"
                for row in bot.sent[-1].reply_markup.inline_keyboard
                for button in row
            )
            assert await club(sessions) == original
            await feed(LEADER, data=action("Назад"))
            assert "Твой ввод: X" in shown(bot.sent[-1])
            await feed(LEADER, text="Исправленное подготовленное имя")
            await feed(LEADER, data=action("Сохранить изменения"))
            assert (await club(sessions))["name"] == "Исправленное подготовленное имя"
        finally:
            await bot.session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("version", [None, True, 0, "1"])
def test_batch_requires_explicit_positive_integer_version(version):
    async def scenario():
        with pytest.raises(ValidationError):
            await ClubService(None).update_profile_fields(
                LEADER, "cinema", {"name": "Имя клуба"}, expected_version=version
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("fields", [{}, {"level": "IPO"}, {"organizers": []}, {"links": []}])
def test_batch_cannot_expand_to_roles_levels_or_unexposed_fields(sessions, fields):
    async def scenario():
        await seed(sessions)
        original = await club(sessions)
        async with sessions.begin() as session:
            with pytest.raises(ValidationError):
                await ClubService(session).update_profile_fields(
                    LEADER, "cinema", fields, expected_version=original["version"]
                )
        assert await club(sessions) == original

    asyncio.run(scenario())
