"""Real PostgreSQL survey acceptance: immutable versions, immediate ratings, late visits."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from test_events import ADMIN, EDITOR, LEADER, OTHER, STUDENT, published, seed
from test_events import sessions as sessions

from cu_clubs.attendance_models import Attendance
from cu_clubs.event_models import Event
from cu_clubs.models import Outbox, Person
from cu_clubs.services import ClubService, PermissionDenied, ValidationError
from cu_clubs.survey_models import SurveyAnswer, SurveyQuestion, SurveyResponse
from cu_clubs.surveys import SurveysService, merge_survey_people, validate_questions


def additions():
    return [
        {"kind": "text", "prompt": "Что улучшить?", "required": False},
        {"kind": "single", "prompt": "Формат", "required": True, "choices": ["Кино", "Лекция"]},
        {
            "kind": "multiple",
            "prompt": "Темы",
            "required": True,
            "choices": ["История", "Искусство"],
        },
        {"kind": "scale", "prompt": "Звук", "required": False, "scale_max": 10},
    ]


async def visit(session, public_id, actor, *, guest=False):
    clubs = ClubService(session)
    account = await clubs._account(actor)
    if guest:
        person = await session.get(Person, account.person_id)
        person.kind, person.verified_at = "guest", None
    event = await session.scalar(select(Event).where(Event.public_id == public_id))
    event.starts_at = datetime.now(UTC) - timedelta(hours=3)
    event.ends_at = datetime.now(UTC) - timedelta(hours=1)
    event.registration_deadline = event.starts_at
    attendance = Attendance(
        event_id=event.id,
        person_id=account.person_id,
        source="event_qr",
        actor_telegram_id=actor,
        full_name_at_mark="Synthetic Visitor",
        guest_at_mark=guest,
        was_bot_registered=True,
        was_registered=False,
        registered_at_start=False,
    )
    session.add(attendance)
    await session.flush()
    return event, account.person_id


def test_validation_fixed_first_and_four_types():
    assert [q["kind"] for q in validate_questions(additions())] == [
        "rating",
        "text",
        "single",
        "multiple",
        "scale",
    ]
    for wrong in [
        [],
        [{"kind": "rating", "prompt": "Other", "required": True, "scale_max": 5}],
        [{"kind": "scale", "prompt": "Q", "scale_max": True}],
        [{"kind": "single", "prompt": "Q", "choices": ["A", "A"]}],
    ]:
        if not wrong:
            assert len(validate_questions(wrong)) == 1
        else:
            with pytest.raises(ValidationError):
                validate_questions(wrong)


def test_rating_immediate_resumable_versioned_guest(sessions):
    async def scenario():
        await seed(sessions)
        async with sessions.begin() as session:
            club = await SurveysService(session).save_club(LEADER, "cinema", additions())
        event = await published(sessions)
        async with sessions.begin() as session:
            service = SurveysService(session)
            initial = await service.get_event(LEADER, event["public_id"])
            assert initial["questions"][1]["prompt"] == club["questions"][1]["prompt"]
            await visit(session, event["public_id"], STUDENT, guest=True)
            response = await service.start_response(STUDENT, event["public_id"])
            first_id = response["next_question"]["id"]
            response = await service.answer(STUDENT, event["public_id"], first_id, 5)
            assert response["rating"] == 5 and not response["completed"]
            assert response["next_question"]["kind"] == "text"
        async with sessions.begin() as session:
            service = SurveysService(session)
            response = await service.start_response(STUDENT, event["public_id"])
            assert response["rating"] == 5
            assert (await service.answer(STUDENT, event["public_id"], first_id, 1))["rating"] == 5
            changed = await service.save_event(
                LEADER,
                event["public_id"],
                [{"kind": "text", "prompt": "New question", "required": False}],
                initial["version"],
            )
            assert changed["version"] == initial["version"] + 1
            response = await service.start_response(STUDENT, event["public_id"])
            assert response["version"] == initial["version"]
            assert response["next_question"]["prompt"] == "Что улучшить?"
            response = await service.answer(
                STUDENT, event["public_id"], response["next_question"]["id"], None
            )
            single = response["next_question"]
            with pytest.raises(ValidationError):
                await service.answer(STUDENT, event["public_id"], single["id"], None)
            response = await service.answer(
                STUDENT, event["public_id"], single["id"], single["choices"][0]["id"]
            )
            multi = response["next_question"]
            response = await service.answer(
                STUDENT, event["public_id"], multi["id"], [c["id"] for c in multi["choices"]]
            )
            scale = response["next_question"]
            with pytest.raises(ValidationError, match="от 1 до 10"):
                await service.answer(STUDENT, event["public_id"], scale["id"], 11)
            response = await service.answer(STUDENT, event["public_id"], scale["id"], 10)
            assert response["completed"]
            assert await session.scalar(select(func.count()).select_from(SurveyResponse)) == 1
            assert await session.scalar(select(func.count()).select_from(SurveyAnswer)) == 5
            assert not await service.pending(STUDENT)

    asyncio.run(scenario())


def test_permissions_end_gate_and_first_db_constraint(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions)
        async with sessions.begin() as session:
            service = SurveysService(session)
            with pytest.raises(PermissionDenied):
                await service.get_club(EDITOR, "cinema")
            with pytest.raises(PermissionDenied):
                await service.get_event(EDITOR, event["public_id"])
            with pytest.raises(ValidationError, match="после окончания"):
                await service.start_response(STUDENT, event["public_id"])
            await visit(session, event["public_id"], STUDENT)
            with pytest.raises(PermissionDenied):
                await service.start_response(OTHER, event["public_id"])
            survey = await service.get_event(ADMIN, event["public_id"])
            first_id = UUID(survey["questions"][0]["id"])
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await session.execute(
                        update(SurveyQuestion)
                        .where(SurveyQuestion.id == first_id)
                        .values(scale_max=10)
                    )
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await session.execute(
                        update(SurveyQuestion)
                        .where(SurveyQuestion.id == first_id)
                        .values(scale_max=None)
                    )

    asyncio.run(scenario())


def test_late_invitation_dedupe_and_identity_merge(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions)
        async with sessions.begin() as session:
            service = SurveysService(session)
            owner, person = await visit(session, event["public_id"], STUDENT)
            assert await service.enqueue_invitations() == 1
            assert await service.enqueue_invitations() == 0
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Outbox)
                    .where(Outbox.kind == "survey.invitation")
                )
                == 1
            )
            response = await service.start_response(STUDENT, event["public_id"])
            await service.answer(STUDENT, event["public_id"], response["next_question"]["id"], 4)
            imported = Person(full_name="Imported synthetic student")
            session.add(imported)
            await session.flush()
            await merge_survey_people(session, person, imported.id)
            saved = await session.scalar(
                select(SurveyResponse).where(SurveyResponse.event_id == owner.id)
            )
            assert saved.person_id == imported.id and saved.rating == 4
            # A second real response cannot erase the original on an identity collision.
            other = await ClubService(session)._account(OTHER)
            duplicate = SurveyResponse(
                event_id=owner.id,
                person_id=other.person_id,
                survey_version_id=saved.survey_version_id,
                rating=1,
            )
            session.add(duplicate)
            await session.flush()
            with pytest.raises(ValidationError, match="обе оценки сохранены"):
                await merge_survey_people(session, other.person_id, imported.id)
            assert duplicate.person_id == other.person_id
            assert saved.rating == 4 and duplicate.rating == 1

    asyncio.run(scenario())


def test_imported_visitor_waits_for_dialog_and_verification(sessions):
    from cu_clubs.models import TelegramAccount

    async def scenario():
        await seed(sessions)
        event = await published(sessions)
        async with sessions.begin() as session:
            owner = await session.scalar(select(Event).where(Event.public_id == event["public_id"]))
            owner.starts_at = datetime.now(UTC) - timedelta(hours=3)
            owner.ends_at = datetime.now(UTC) - timedelta(hours=1)
            owner.registration_deadline = owner.starts_at
            imported = Person(full_name="Synthetic import", kind="student")
            session.add(imported)
            await session.flush()
            session.add(
                Attendance(
                    event_id=owner.id,
                    person_id=imported.id,
                    source="import",
                    actor_telegram_id=LEADER,
                    full_name_at_mark=imported.full_name,
                    guest_at_mark=False,
                    was_bot_registered=False,
                    was_registered=False,
                    registered_at_start=False,
                )
            )
            await session.flush()
            service = SurveysService(session)
            assert await service.enqueue_invitations() == 0
            session.add(TelegramAccount(telegram_id=91999, person_id=imported.id))
            await session.flush()
            assert await service.enqueue_invitations() == 0
            imported.verified_at = datetime.now(UTC)
            await session.flush()
            assert await service.enqueue_invitations() == 1
            assert await service.enqueue_invitations() == 0

    asyncio.run(scenario())
