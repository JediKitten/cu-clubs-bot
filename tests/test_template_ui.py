"""Persistent template dialogues against real PostgreSQL, with no external delivery."""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from test_events import ADMIN, EDITOR, LEADER, OTHER, PARTNER, fields, seed
from test_events import sessions as sessions

from cu_clubs.event_models import Event, EventTemplate
from cu_clubs.events import EventService
from cu_clubs.models import Conversation, Outbox
from cu_clubs.services import ClubService, PermissionDenied, ValidationError
from cu_clubs.template_ui import handle_templates

MOSCOW = ZoneInfo("Europe/Moscow")


def action(screen, prefix):
    return next(
        button.callback_data
        for row in screen.markup.inline_keyboard
        for button in row
        if (button.callback_data or "").startswith(prefix)
    )


async def step(factory, data="", *, raw=None, actor=LEADER):
    async with factory.begin() as session:
        state = await session.get(Conversation, actor)
        if state is None:
            state = Conversation(telegram_id=actor, state="idle", payload={})
            session.add(state)
            await session.flush()
        screen = await handle_templates(
            session,
            actor,
            state,
            SimpleNamespace(text=raw) if raw is not None else None,
            data,
            SimpleNamespace(),
        )
        if screen and screen.markup:
            for row in screen.markup.inline_keyboard:
                for button in row:
                    assert button.callback_data and len(button.callback_data.encode()) <= 64
        return screen


async def source(factory, *, collaboration=False):
    start = datetime(2030, 4, 5, 23, 30, tzinfo=MOSCOW)
    async with factory.begin() as session:
        service = EventService(session)
        event = await service.create_draft(
            LEADER,
            "cinema",
            fields(
                starts_at=start,
                ends_at=start + timedelta(hours=2),
                room="305",
                needs_talk=True,
            ),
        )
        if collaboration:
            pending = await service.invite_partner(
                LEADER, event["public_id"], "p" * 32, event["version"]
            )
            return await service.respond_invitation(PARTNER, pending["invitation_id"], True)
        return await service.publish(LEADER, event["public_id"], event["version"])


async def template(factory, event, name="Шаблон показа"):
    async with factory.begin() as session:
        return await EventService(session).save_template(
            LEADER, event["public_id"], name, target_club_public_id="cinema"
        )


async def read_template(factory, public_id):
    async with factory() as session:
        return await EventService(session).get_template(LEADER, public_id)


def test_template_card_edit_and_rename_require_confirm_and_leave_source_unchanged(sessions):
    async def scenario():
        await seed(sessions)
        event = await source(sessions)
        item = await template(sessions, event, "<Показ> & кино")
        listing = await step(sessions, "evtemplates:cinema")
        card = await step(sessions, action(listing, "tmpl:"))
        assert "&lt;Показ&gt; &amp; кино" in card.text
        assert "23:30 по Москве" in card.text and "120 мин." in card.text
        assert "2030" not in card.text and "305" not in card.text
        edit = await step(sessions, action(card, "tmedit:"))
        assert (
            len(
                [
                    button
                    for row in edit.markup.inline_keyboard
                    for button in row
                    if button.callback_data.startswith("tmg:")
                ]
            )
            == 4
        )
        group = await step(sessions, action(edit, "tmg:"))
        await step(sessions, action(group, "tmfield:").replace(":name", ":desc"))
        preview = await step(sessions, raw="Новое <b>описание</b>")
        assert "&lt;b&gt;описание&lt;/b&gt;" in preview.text
        assert (await read_template(sessions, item["public_id"]))["fields"]["description"] == event[
            "description"
        ]
        updated = await step(sessions, action(preview, "tmsave:"))
        await step(sessions, action(updated, "tmrename:"))
        preview = await step(sessions, raw="{{.Name}} <Новое>")
        assert (await read_template(sessions, item["public_id"]))["name"] == item["name"]
        renamed = await step(sessions, action(preview, "tmsave:"))
        assert "{{.Name}} &lt;Новое&gt;" in renamed.text
        current = await read_template(sessions, item["public_id"])
        assert current["fields"]["description"] == "Новое <b>описание</b>"
        async with sessions() as session:
            unchanged = await EventService(session).get_event(event["public_id"], LEADER)
            assert unchanged["description"] == event["description"]
            assert unchanged["version"] == event["version"]

    asyncio.run(scenario())


def test_template_editor_group_back_preview_back_cancel_and_legacy_direct_field(sessions):
    async def scenario():
        await seed(sessions)
        event = await source(sessions)
        item = await template(sessions, event)
        card = await step(sessions, f"tmpl:{item['public_id']}")
        groups = await step(sessions, action(card, "tmedit:"))
        group_buttons = [
            button
            for row in groups.markup.inline_keyboard
            for button in row
            if button.callback_data.startswith("tmg:")
        ]
        assert len(group_buttons) == 4
        counts = []
        group_screens = []
        for group_button in group_buttons:
            group_screen = await step(sessions, group_button.callback_data)
            group_screens.append(group_screen)
            counts.append(
                sum(
                    button.callback_data.startswith("tmfield:")
                    for row in group_screen.markup.inline_keyboard
                    for button in row
                )
            )
        assert max(counts) <= 4
        back_to_groups = await step(sessions, action(group_screens[0], "tmedit:"))
        assert "Изменить шаблон" in back_to_groups.text

        # Old deep links still open the matching field prompt.
        prompt = await step(sessions, f"tmfield:{item['public_id']}:clock")
        assert "19:30" in prompt.text
        assert action(prompt, "tmg:").endswith(":1")
        preview = await step(sessions, raw="19.30")
        assert "19:30 по Москве" in preview.text
        resumed = await step(sessions, action(preview, "tmback:"))
        assert "Текущее значение: 19.30" in resumed.text
        assert resumed.markup.inline_keyboard[-2][0].callback_data.endswith(":1")
        with pytest.raises(ValidationError, match="Не распознал время"):
            await step(sessions, raw="25:80")
        async with sessions() as session:
            state = await session.get(Conversation, LEADER)
            assert state.state == "template_edit"
            assert state.payload["field"] == "start_time"
            assert state.payload["group"] == 1
        # Cancel from field input discards the draft and returns to its card.
        returned = await step(sessions, action(resumed, "tmpl:"))
        assert "Шаблон:" in returned.text
        assert (await read_template(sessions, item["public_id"]))["fields"]["start_time"] == "23:30"

    asyncio.run(scenario())


def test_template_clock_accepts_flexible_forms_and_normalizes_to_hhmm(sessions):
    async def scenario():
        await seed(sessions)
        event = await source(sessions)
        item = await template(sessions, event)
        for raw, expected in (("19.30", "19:30"), ("1930", "19:30"), ("19", "19:00")):
            await step(sessions, f"tmfield:{item['public_id']}:clock")
            preview = await step(sessions, raw=raw)
            assert f"{expected} по Москве" in preview.text
            card = await step(sessions, action(preview, "tmsave:"))
            assert f"{expected} по Москве" in card.text

    asyncio.run(scenario())


def test_preview_back_rotates_template_token_and_rejects_old_field_and_save_callbacks(sessions):
    async def scenario():
        await seed(sessions)
        event = await source(sessions)
        item = await template(sessions, event)
        prompt = await step(sessions, f"tmfield:{item['public_id']}:clock")
        async with sessions() as session:
            state = await session.get(Conversation, LEADER)
            old_token = state.payload["token"]
        old_choice = action(prompt, "tmchoice:")
        first_preview = await step(sessions, raw="19.30")
        old_save = action(first_preview, "tmsave:")
        returned = await step(sessions, action(first_preview, "tmback:"))
        assert "Текущее значение: 19.30" in returned.text
        async with sessions() as session:
            state = await session.get(Conversation, LEADER)
            new_token = state.payload["token"]
        assert new_token != old_token
        with pytest.raises(ValidationError, match="устарела"):
            await step(sessions, old_save)

        second_preview = await step(sessions, raw="20")
        with pytest.raises(ValidationError, match="устарела"):
            await step(sessions, old_choice)
        with pytest.raises(ValidationError, match="устарела"):
            await step(sessions, old_save)
        assert "20:00 по Москве" in second_preview.text
        await step(sessions, action(second_preview, "tmsave:"))
        assert (await read_template(sessions, item["public_id"]))["fields"]["start_time"] == "20:00"

    asyncio.run(scenario())


def test_optional_clock_and_duration_can_be_cleared_without_losing_other_fields(sessions):
    async def scenario():
        await seed(sessions)
        event = await source(sessions)
        item = await template(sessions, event)
        for code in ("clock", "duration"):
            await step(sessions, f"tmfield:{item['public_id']}:{code}")
            async with sessions() as session:
                state = await session.get(Conversation, LEADER)
                token = state.payload["token"]
            preview = await step(sessions, f"tmchoice:{token}:clear")
            await step(sessions, action(preview, "tmsave:"))
        current = await read_template(sessions, item["public_id"])
        assert "start_time" not in current["fields"]
        assert "duration_minutes" not in current["fields"]
        assert current["fields"]["title"] == event["title"]
        card = await step(sessions, f"tmpl:{item['public_id']}")
        screen = await step(sessions, action(card, "tmuse:"))
        assert "Указать дату и время" in str(screen.markup)
        async with sessions() as session:
            draft = await session.scalar(select(Event).where(Event.status == "draft"))
            assert draft.template_start_time is None and draft.template_duration_minutes is None

    asyncio.run(scenario())


def test_create_from_template_is_versioned_one_use_and_shows_one_date_action(sessions):
    async def scenario():
        await seed(sessions)
        event = await source(sessions)
        item = await template(sessions, event)
        card = await step(sessions, f"evfromtemplate:{item['public_id']}")
        old_action = action(card, "tmuse:")
        async with sessions.begin() as session:
            await EventService(session).rename_template(
                EDITOR, item["public_id"], "Обновлённый", item["version"]
            )
        with pytest.raises(ValidationError, match="изменился"):
            await step(sessions, old_action)
        card = await step(sessions, f"tmpl:{item['public_id']}")
        create = action(card, "tmuse:")
        async with sessions() as session:
            before_outbox = await session.scalar(select(func.count()).select_from(Outbox))
        screen = await step(sessions, create)
        date_buttons = [
            button
            for row in screen.markup.inline_keyboard
            for button in row
            if button.callback_data.endswith(":start")
        ]
        assert len(date_buttons) == 1
        with pytest.raises(ValidationError, match="устарела"):
            await step(sessions, create)
        async with sessions() as session:
            drafts = list(await session.scalars(select(Event).where(Event.status == "draft")))
            assert len(drafts) == 1
            draft = drafts[0]
            assert draft.starts_at is None and draft.ends_at is None and draft.room is None
            assert draft.template_start_time == "23:30" and draft.template_duration_minutes == 120
            assert draft.public_id != event["public_id"]
            assert await session.scalar(select(func.count()).select_from(Outbox)) == before_outbox

    asyncio.run(scenario())


def test_stale_and_replaced_delete_confirmations_cannot_delete_current_template(sessions):
    async def scenario():
        await seed(sessions)
        event = await source(sessions)
        first = await template(sessions, event, "Первый")
        second = await template(sessions, event, "Второй")
        prompt = await step(sessions, f"tmdelask:{first['public_id']}")
        old_delete = action(prompt, "tmdelete:")
        async with sessions.begin() as session:
            await EventService(session).rename_template(
                EDITOR, first["public_id"], "Новый первый", first["version"]
            )
        with pytest.raises(ValidationError, match="изменился"):
            await step(sessions, old_delete)
        await step(sessions, f"tmdelask:{second['public_id']}")
        with pytest.raises(ValidationError, match="устарела"):
            await step(sessions, old_delete)
        prompt = await step(sessions, f"tmdelask:{first['public_id']}")
        result = await step(sessions, action(prompt, "tmdelete:"))
        assert "встречи сохранены" in result.text
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(Event)) == 1
            deleted = await session.scalar(
                select(EventTemplate).where(EventTemplate.public_id == first["public_id"])
            )
            assert deleted.deleted_at is not None
        assert (await read_template(sessions, second["public_id"]))["name"] == "Второй"

    asyncio.run(scenario())


def test_revoked_management_is_rechecked_on_preview_and_confirmation(sessions):
    async def scenario():
        await seed(sessions)
        event = await source(sessions)
        item = await template(sessions, event)
        await step(sessions, f"tmrename:{item['public_id']}", actor=EDITOR)
        preview = await step(sessions, raw="Нельзя сохранить", actor=EDITOR)
        confirm = action(preview, "tmsave:")
        async with sessions.begin() as session:
            await ClubService(session).remove_organizer(LEADER, "cinema", EDITOR)
        with pytest.raises(PermissionDenied):
            await step(sessions, confirm, actor=EDITOR)
        with pytest.raises(PermissionDenied):
            await step(sessions, "tmpls:cinema", actor=OTHER)
        assert (await read_template(sessions, item["public_id"]))["name"] == item["name"]

    asyncio.run(scenario())


def test_joint_event_can_be_saved_by_each_team_only_to_its_own_club(sessions):
    async def scenario():
        await seed(sessions)
        event = await source(sessions, collaboration=True)
        for actor, club in ((LEADER, "cinema"), (PARTNER, "p" * 32)):
            entry = await step(sessions, f"evtemplate:{event['public_id']}", actor=actor)
            assert entry.text == "Как назвать шаблон?"
            preview = await step(sessions, raw="Общий показ", actor=actor)
            assert "23:30" in preview.text and "120 мин." in preview.text
            saved = await step(sessions, action(preview, "tmsave_source:"), actor=actor)
            assert "Шаблон сохранён" in saved.text
            async with sessions() as session:
                items = await EventService(session).templates(actor, club)
                assert len(items) == 1 and items[0]["name"] == "Общий показ"
        chooser = await step(sessions, f"evtemplate:{event['public_id']}", actor=ADMIN)
        assert "какого клуба" in chooser.text
        await step(sessions, action(chooser, "tmtarget:"), actor=ADMIN)
        preview = await step(sessions, raw="Выбор администратора", actor=ADMIN)
        assert preview and "Предпросмотр" in preview.text

    asyncio.run(scenario())


def test_source_change_after_preview_rejects_save_without_creating_template(sessions):
    async def scenario():
        await seed(sessions)
        event = await source(sessions)
        await step(sessions, f"evtemplate:{event['public_id']}")
        preview = await step(sessions, raw="Отмена устаревшего")
        async with sessions.begin() as session:
            await EventService(session).update_event(
                LEADER, event["public_id"], {"room": "310"}, event["version"]
            )
        with pytest.raises(ValidationError, match="изменил|изменилась"):
            await step(sessions, action(preview, "tmsave_source:"))
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(EventTemplate)) == 0

    asyncio.run(scenario())


def test_lost_destination_right_is_rejected_before_asking_for_a_name(sessions):
    async def scenario():
        await seed(sessions)
        event = await source(sessions, collaboration=True)
        partner = "p" * 32
        async with sessions.begin() as session:
            await ClubService(session).set_organizer(
                PARTNER,
                partner,
                LEADER,
                edit_profile=False,
                manage_events=True,
                record_attendance=False,
            )
        chooser = await step(sessions, f"evtemplate:{event['public_id']}")
        select_partner = next(
            button.callback_data
            for row in chooser.markup.inline_keyboard
            for button in row
            if button.text == "Второй клуб"
        )
        async with sessions.begin() as session:
            await ClubService(session).remove_organizer(PARTNER, partner, LEADER)
        with pytest.raises(ValidationError, match="недоступен"):
            await step(sessions, select_partner)
        async with sessions() as session:
            state = await session.get(Conversation, LEADER)
            assert state.state == "template_source_target"
            assert "target" not in state.payload
            assert await session.scalar(select(func.count()).select_from(EventTemplate)) == 0

    asyncio.run(scenario())


def test_list_paginates_without_creating_drafts_and_handles_empty_club(sessions):
    async def scenario():
        await seed(sessions)
        assert "Шаблонов пока нет" in (await step(sessions, "tmpls:cinema")).text
        event = await source(sessions)
        async with sessions.begin() as session:
            service = EventService(session)
            for index in range(17):
                await service.save_template(LEADER, event["public_id"], f"Показ {index:02}")
        first = await step(sessions, "tmpls:cinema")
        assert (
            len(
                [
                    button
                    for row in first.markup.inline_keyboard
                    for button in row
                    if button.callback_data.startswith("tmpl:")
                ]
            )
            == 15
        )
        second = await step(sessions, action(first, "tmpls:cinema:1"))
        assert (
            len(
                [
                    button
                    for row in second.markup.inline_keyboard
                    for button in row
                    if button.callback_data.startswith("tmpl:")
                ]
            )
            == 2
        )
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(Event)) == 1

    asyncio.run(scenario())
