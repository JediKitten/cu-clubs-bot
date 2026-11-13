"""Portable club templates preserve history and enforce current team permissions."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select
from test_events import ADMIN, EDITOR, LEADER, OTHER, PARTNER, STUDENT, fields, seed
from test_events import sessions as sessions

from cu_clubs.event_models import Event, EventClub, EventInvitation, EventTemplate
from cu_clubs.events import EventService, validate_template_fields
from cu_clubs.models import Audit, Club, Membership, Outbox
from cu_clubs.services import ClubService, PermissionDenied, ValidationError


async def source(factory, **overrides):
    async with factory.begin() as session:
        start = datetime(2030, 1, 10, 20, 30, tzinfo=UTC)
        return await EventService(session).create_draft(
            LEADER,
            "cinema",
            fields(starts_at=start, ends_at=start + timedelta(hours=2), **overrides),
        )


@pytest.mark.parametrize(
    "patch",
    [
        {"starts_at": "2030-01-01T12:00:00+00:00"},
        {"room": "B714"},
        {"public_id": "old-link"},
        {"duration_minutes": True},
        {"duration_minutes": 0},
        {"duration_minutes": 2**31},
        {"start_time": "24:00"},
        {"start_time": "12:60"},
        {"start_time": "9:00"},
    ],
)
def test_template_rejects_nonportable_or_invalid_settings(patch):
    with pytest.raises(ValidationError):
        validate_template_fields(patch)


def test_copied_meeting_preserves_settings_but_has_its_own_dates_and_identity(sessions):
    async def scenario():
        await seed(sessions)
        event = await source(sessions, room="B714", needs_talk=True)
        async with sessions.begin() as session:
            service = EventService(session)
            event = await service.publish(LEADER, event["public_id"], event["version"])
            await service.register(STUDENT, event["public_id"])
            queued = await session.scalar(select(func.count()).select_from(Outbox))
            template = await service.save_template(
                LEADER,
                event["public_id"],
                "Вечерний показ",
                expected_event_version=event["version"],
            )
            copied = await service.from_template(
                EDITOR, template["public_id"], expected_version=template["version"]
            )
            assert template["fields"]["start_time"] == "23:30"
            assert template["fields"]["duration_minutes"] == 120
            assert copied["title"] == event["title"]
            assert copied["post_registration_text"] == event["post_registration_text"]
            assert copied["needs_talk"] and copied["capacity"] == 2
            assert copied["status"] == "draft" and copied["public_id"] != event["public_id"]
            assert copied["registered_count"] == 0 and len(copied["clubs"]) == 1
            assert all(
                copied[key] is None
                for key in ("starts_at", "ends_at", "registration_deadline", "room")
            )
            assert await session.scalar(select(func.count()).select_from(Outbox)) == queued
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(EventInvitation)
                    .where(EventInvitation.event_id == UUID(copied["id"]))
                )
                == 0
            )
        async with sessions.begin() as session:
            stored = await session.get(Event, UUID(copied["id"]))
            assert stored.template_start_time == "23:30"
            assert stored.template_duration_minutes == 120
            current = await EventService(session).get_event(event["public_id"], LEADER)
            assert datetime.fromisoformat(current["starts_at"]) == datetime.fromisoformat(
                event["starts_at"]
            )
            assert current["room"] == "B714" and current["registered_count"] == 1

    asyncio.run(scenario())


def test_crud_versions_audit_and_existing_meetings_are_independent(sessions):
    async def scenario():
        await seed(sessions)
        event = await source(sessions)
        async with sessions.begin() as session:
            service = EventService(session)
            template = await service.save_template(LEADER, event["public_id"], "Показ")
            copied = await service.from_template(LEADER, template["public_id"])
            renamed = await service.rename_template(
                EDITOR, template["public_id"], "Дискуссия", template["version"]
            )
            with pytest.raises(ValidationError, match="изменился"):
                await service.update_template(
                    LEADER, template["public_id"], {"capacity": 10}, template["version"]
                )
            changed = await service.update_template(
                EDITOR,
                template["public_id"],
                {"capacity": 10, "start_time": None},
                renamed["version"],
            )
            assert changed["version"] == renamed["version"] + 1
            assert "start_time" not in changed["fields"]
            assert (await service.get_event(copied["public_id"], LEADER))["capacity"] == 2
            with pytest.raises(ValidationError, match="изменился"):
                await service.delete_template(LEADER, template["public_id"], renamed["version"])
            await service.delete_template(LEADER, template["public_id"], changed["version"])
            assert await service.templates(EDITOR, "cinema") == []
            assert (await service.get_event(copied["public_id"], LEADER))["title"] == event["title"]
            replacement = await service.save_template(LEADER, event["public_id"], "Дискуссия")
            assert replacement["public_id"] != template["public_id"]
            old = await session.scalar(
                select(EventTemplate).where(EventTemplate.public_id == template["public_id"])
            )
            assert old.deleted_at is not None and old.fields["capacity"] == 10
            actions = set(
                await session.scalars(select(Audit.action).where(Audit.entity_id == old.id))
            )
            assert {
                "template.created",
                "template.used",
                "template.renamed",
                "template.updated",
                "template.deleted",
            } <= actions
            with pytest.raises(ValidationError, match="не найден"):
                await service.get_template(LEADER, template["public_id"])
            with pytest.raises(ValidationError, match="не найден"):
                await service.from_template(LEADER, template["public_id"])

    asyncio.run(scenario())


def test_every_template_command_rechecks_revoked_permissions_and_other_clubs(sessions):
    async def scenario():
        await seed(sessions)
        event = await source(sessions)
        async with sessions.begin() as session:
            service = EventService(session)
            template = await service.save_template(EDITOR, event["public_id"], "Командный")
            clubs = ClubService(session)
            attendance_editor = await clubs._account(OTHER)
            cinema = await clubs._club("cinema")
            session.add(
                Membership(
                    club_id=cinema.id,
                    person_id=attendance_editor.person_id,
                    role="organizer",
                    edit_profile=True,
                    record_attendance=True,
                    manage_events=False,
                )
            )
            await session.flush()
            for actor in (STUDENT, PARTNER, OTHER):
                with pytest.raises(PermissionDenied):
                    await service.get_template(actor, template["public_id"])
            await ClubService(session).remove_organizer(LEADER, "cinema", EDITOR)
        for command in (
            lambda service: service.get_template(EDITOR, template["public_id"]),
            lambda service: service.templates(EDITOR, "cinema"),
            lambda service: service.from_template(EDITOR, template["public_id"]),
            lambda service: service.rename_template(
                EDITOR, template["public_id"], "Другой", template["version"]
            ),
            lambda service: service.update_template(
                EDITOR, template["public_id"], {"capacity": 5}, template["version"]
            ),
            lambda service: service.delete_template(
                EDITOR, template["public_id"], template["version"]
            ),
        ):
            async with sessions.begin() as session:
                with pytest.raises(PermissionDenied):
                    await command(EventService(session))

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["save", "rename"])
def test_parallel_same_name_returns_one_success_and_one_validation_error(sessions, operation):
    async def scenario():
        await seed(sessions)
        sources = [await source(sessions), await source(sessions)]
        templates = []
        if operation == "rename":
            async with sessions.begin() as session:
                service = EventService(session)
                templates = [
                    await service.save_template(LEADER, e["public_id"], f"Исходный {index}")
                    for index, e in enumerate(sources)
                ]

        async def attempt(index):
            try:
                async with sessions.begin() as session:
                    service = EventService(session)
                    if operation == "save":
                        await service.save_template(
                            LEADER, sources[index]["public_id"], "Общее имя"
                        )
                    else:
                        template = templates[index]
                        await service.rename_template(
                            LEADER, template["public_id"], "Общее имя", template["version"]
                        )
                return "success"
            except ValidationError as error:
                assert "названием уже существует" in str(error)
                return "duplicate"

        assert sorted(await asyncio.gather(attempt(0), attempt(1))) == ["duplicate", "success"]
        async with sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(EventTemplate)
                    .where(EventTemplate.name == "Общее имя", EventTemplate.deleted_at.is_(None))
                )
                == 1
            )

    asyncio.run(scenario())


def test_collaboration_teams_save_to_their_own_club_not_an_unrelated_club(sessions):
    async def scenario():
        await seed(sessions)
        event = await source(sessions)
        async with sessions.begin() as session:
            service = EventService(session)
            pending = await service.invite_partner(
                LEADER, event["public_id"], "p" * 32, event["version"]
            )
            with pytest.raises(PermissionDenied):
                await service.save_template(PARTNER, event["public_id"], "Рано")
            accepted = await service.respond_invitation(PARTNER, pending["invitation_id"], True)
            assert {
                item["public_id"]
                for item in await service.template_destinations(PARTNER, event["public_id"])
            } == {"p" * 32}
            own = await service.save_template(PARTNER, event["public_id"], "Партнёрский")
            assert own["club_public_id"] == "p" * 32
            with pytest.raises(PermissionDenied):
                await service.save_template(
                    PARTNER, event["public_id"], "Чужой", target_club_public_id="cinema"
                )
            assert len(await service.template_destinations(ADMIN, event["public_id"])) == 2
            with pytest.raises(ValidationError, match="Выбери клуб"):
                await service.save_template(ADMIN, event["public_id"], "Без выбора")
            unrelated = Club(public_id="unrelated", name="Третий", level_name="Seed")
            session.add(unrelated)
            await session.flush()
            with pytest.raises(PermissionDenied):
                await service.save_template(
                    ADMIN, event["public_id"], "Не участник", target_club_public_id="unrelated"
                )
            copied = await service.from_template(PARTNER, own["public_id"])
            assert copied["clubs"] == [
                {"public_id": "p" * 32, "name": "Второй клуб", "level": "Seed"}
            ]
            assert copied["status"] == "draft" and accepted["status"] == "published"
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(EventClub)
                    .where(EventClub.event_id == UUID(copied["id"]))
                )
                == 1
            )

    asyncio.run(scenario())


def test_stale_source_and_stale_template_cannot_silently_copy_another_revision(sessions):
    async def scenario():
        await seed(sessions)
        event = await source(sessions)
        async with sessions.begin() as session:
            service = EventService(session)
            changed = await service.update_draft(
                EDITOR, event["public_id"], {"description": "Новая программа"}, event["version"]
            )
            with pytest.raises(ValidationError, match="Карточку уже изменил"):
                await service.save_template(
                    LEADER, event["public_id"], "Старый", expected_event_version=event["version"]
                )
            template = await service.save_template(
                LEADER, event["public_id"], "Актуальный", expected_event_version=changed["version"]
            )
            await service.update_template(
                EDITOR, template["public_id"], {"needs_talk": True}, template["version"]
            )
            total = await session.scalar(select(func.count()).select_from(Event))
            with pytest.raises(ValidationError, match="изменился"):
                await service.from_template(
                    LEADER, template["public_id"], expected_version=template["version"]
                )
            assert await session.scalar(select(func.count()).select_from(Event)) == total

    asyncio.run(scenario())


def test_duration_survives_restart_sets_first_ending_and_respects_manual_choice(sessions):
    async def scenario():
        await seed(sessions)
        event = await source(sessions)
        async with sessions.begin() as session:
            service = EventService(session)
            template = await service.save_template(LEADER, event["public_id"], "Ночной")
            copied = await service.from_template(LEADER, template["public_id"])
            other = await service.from_template(LEADER, template["public_id"])
        async with sessions.begin() as session:
            service = EventService(session)
            new_start = datetime(2031, 5, 5, 20, 30, tzinfo=UTC)
            dated = await service.update_draft(
                LEADER, copied["public_id"], {"starts_at": new_start}, copied["version"]
            )
            assert datetime.fromisoformat(dated["ends_at"]) == new_start + timedelta(hours=2)
            manual = await service.update_draft(
                LEADER,
                copied["public_id"],
                {"ends_at": new_start + timedelta(hours=3)},
                dated["version"],
            )
            assert manual["template_duration_minutes"] is None
            moved = await service.update_draft(
                LEADER,
                copied["public_id"],
                {"starts_at": new_start + timedelta(hours=1)},
                manual["version"],
            )
            assert moved["ends_at"] == manual["ends_at"]
            explicit = await service.update_draft(
                LEADER,
                other["public_id"],
                {"ends_at": new_start + timedelta(hours=4)},
                other["version"],
            )
            explicit = await service.update_draft(
                LEADER, other["public_id"], {"starts_at": new_start}, explicit["version"]
            )
            assert datetime.fromisoformat(explicit["ends_at"]) == new_start + timedelta(hours=4)

    asyncio.run(scenario())
