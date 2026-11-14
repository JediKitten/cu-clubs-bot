"""Survey builder/back routes, multi-select confirmation and persisted first score."""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_events import LEADER, STUDENT, published, seed
from test_events import sessions as sessions
from test_surveys import visit

from cu_clubs.analytics_ui import handle_analytics
from cu_clubs.services import ClubService, PermissionDenied, ValidationError
from cu_clubs.survey_models import SurveyResponse
from cu_clubs.survey_ui import handle_surveys
from cu_clubs.surveys import SurveysService, first_question


def callback(screen, label):
    return next(
        button.callback_data
        for row in screen.markup.inline_keyboard
        for button in row
        if button.text == label
    )


def test_builder_preview_multiselect_resume_and_analytics(sessions):
    async def scenario():
        await seed(sessions)
        state, settings = SimpleNamespace(state="idle", payload={}), SimpleNamespace()
        async with sessions.begin() as session:

            async def click(data="", message=None):
                return await handle_surveys(session, LEADER, state, message, data, settings)

            screen = await click("suclub:cinema")
            assert callback(screen, "Назад") == "manageclub:cinema"
            screen = await click(callback(screen, "Добавить вопрос"))
            screen = await click(callback(screen, "Множественный выбор"))
            screen = await click(message="Что понравилось?")
            screen = await click(message="Обсуждение\nФильм\nЗвук")
            screen = await click(callback(screen, "Необязательный"))
            assert len(state.payload["questions"]) == 2
            old_save = callback(screen, "Сохранить для клуба")
            preview = await click(callback(screen, "Предпросмотр"))
            preview = await click(callback(preview, "5"))
            preview = await click(callback(preview, "Фильм"))
            assert state.payload["index"] == 1
            screen = await click(callback(preview, "Готово"))
            assert state.state == "survey_edit" and len(state.payload["questions"]) == 2
            with pytest.raises(ValidationError, match="устарела"):
                await click(old_save)
            await click(callback(screen, "Сохранить для клуба"))
        event = await published(sessions)
        async with sessions.begin() as session:
            await visit(session, event["public_id"], STUDENT, guest=True)

            async def respond(data="", message=None):
                return await handle_surveys(session, STUDENT, state, message, data, settings)

            screen = await respond("su:" + event["public_id"])
            screen = await respond(callback(screen, "4"))
            stored = await session.scalar(select(SurveyResponse))
            assert stored.rating == 4 and stored.completed_at is None
            selected = await respond(callback(screen, "Фильм"))
            assert "✓ Фильм" in [
                button.text for row in selected.markup.inline_keyboard for button in row
            ]
            resumed = await respond("su:" + event["public_id"])
            assert state.payload["selected"] == []
            done = await respond(callback(resumed, "Пропустить"))
            assert done.text == "Спасибо! Ответы сохранены"
            assert (await SurveysService(session).start_response(STUDENT, event["public_id"]))[
                "rating"
            ] == 4
            report = await handle_analytics(
                session, LEADER, state, None, "anevent:" + event["public_id"], settings
            )
            assert "4,0 из 5" in report.text and "всего посетили 1" in report.text

    asyncio.run(scenario())


def test_club_feedback_back_restores_report_range_and_rechecks_revoked_role(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions)
        state, settings = SimpleNamespace(state="idle", payload={}), SimpleNamespace()
        async with sessions.begin() as session:
            await visit(session, event["public_id"], STUDENT, guest=True)
            surveys = SurveysService(session)
            await surveys.save_event(
                LEADER,
                event["public_id"],
                [
                    first_question(),
                    {
                        "kind": "text",
                        "prompt": "Что понравилось?",
                        "required": False,
                    },
                ],
            )
            response = await surveys.start_response(STUDENT, event["public_id"])
            response = await surveys.answer(
                STUDENT, event["public_id"], response["next_question"]["id"], 5
            )
            await surveys.answer(
                STUDENT, event["public_id"], response["next_question"]["id"], "Хорошо"
            )
            screen = await handle_analytics(session, LEADER, state, None, "anclub:cinema", settings)
            report = await handle_analytics(
                session, LEADER, state, None, callback(screen, "Всё время"), settings
            )
            nonce = state.payload["nonce"]
            saved_report = state.payload["report"]
            saved_range = (state.payload["range_start"], state.payload["range_end"])
            assert state.payload["scope"] == "club" and state.payload["target"] == "cinema"
            assert "Период: Всё время" in report.text
            feedback = await handle_analytics(
                session, LEADER, state, None, callback(report, "Обратная связь"), settings
            )
            restored = await handle_analytics(
                session, LEADER, state, None, callback(feedback, "Назад"), settings
            )
            assert restored.text == report.text
            assert state.payload["nonce"] == nonce
            assert state.payload["report"] == saved_report
            assert (state.payload["range_start"], state.payload["range_end"]) == saved_range

            filter_screen = await handle_analytics(
                session, LEADER, state, None, callback(restored, "Изменить период"), settings
            )
            assert filter_screen.text == "Выбери период"
            assert state.payload["scope"] == "club" and state.payload["target"] == "cinema"
            assert (state.payload["range_start"], state.payload["range_end"]) == saved_range

            clubs = ClubService(session)
            club = await clubs._club("cinema")
            account = await clubs._account(LEADER)
            membership = await clubs._membership(club.id, account.person_id)
            await session.delete(membership)
        async with sessions.begin() as session:
            with pytest.raises(PermissionDenied):
                await handle_analytics(session, LEADER, state, None, f"anreport:{nonce}", settings)

    asyncio.run(scenario())


def test_builder_payload_survives_transactions(sessions):
    from cu_clubs.models import Conversation

    async def scenario():
        await seed(sessions)
        settings = SimpleNamespace()

        async def click(data="", message=None):
            async with sessions.begin() as session:
                state = await session.get(Conversation, LEADER)
                if state is None:
                    state = Conversation(telegram_id=LEADER, state="idle", payload={})
                    session.add(state)
                return await handle_surveys(session, LEADER, state, message, data, settings)

        screen = await click("suclub:cinema")
        screen = await click(callback(screen, "Добавить вопрос"))
        screen = await click(callback(screen, "Текст"))
        screen = await click(message="Durable question")
        screen = await click(callback(screen, "Необязательный"))
        await click(callback(screen, "Сохранить для клуба"))
        async with sessions.begin() as session:
            survey = await SurveysService(session).get_club(LEADER, "cinema")
            assert survey["questions"][1]["prompt"] == "Durable question"

    asyncio.run(scenario())


def test_builder_nonce_rotation_rejects_stale_question_and_preview_callbacks():
    async def scenario():
        questions = [
            first_question(),
            {
                "id": "q-one",
                "kind": "text",
                "prompt": "Первый дополнительный вопрос",
                "choices": [],
                "required": False,
                "scale_max": None,
            },
            {
                "id": "q-two",
                "kind": "text",
                "prompt": "Второй дополнительный вопрос",
                "choices": [],
                "required": False,
                "scale_max": None,
            },
        ]
        state = SimpleNamespace(
            state="survey_edit",
            payload={
                "scope": "club",
                "target": "cinema",
                "version": 1,
                "questions": questions,
                "nonce": "builder-old",
            },
        )
        settings = SimpleNamespace()
        menu_one = await handle_surveys(None, LEADER, state, None, "suq:builder-old:1", settings)
        menu_one_token = state.payload["nonce"]
        stale_edit_one = callback(menu_one, "Изменить")
        stale_return = callback(menu_one, "Назад")
        editor = await handle_surveys(None, LEADER, state, None, stale_return, settings)
        assert "Опрос клуба" in editor.text
        editor_token = state.payload["nonce"]
        assert editor_token != menu_one_token

        menu_two = await handle_surveys(
            None, LEADER, state, None, f"suq:{editor_token}:2", settings
        )
        assert "Второй дополнительный вопрос" in menu_two.text
        with pytest.raises(ValidationError, match="устар"):
            await handle_surveys(None, LEADER, state, None, stale_edit_one, settings)

        preview = await handle_surveys(
            None,
            LEADER,
            state,
            None,
            f"supreview:{state.payload['nonce']}",
            settings,
        )
        first_preview_token = state.payload["nonce"]
        stale_answer = callback(preview, "5")
        stale_exit = callback(preview, "Назад")
        next_preview = await handle_surveys(None, LEADER, state, None, stale_answer, settings)
        assert "Первый дополнительный вопрос" in next_preview.text
        assert state.payload["nonce"] != first_preview_token
        final_preview = await handle_surveys(
            None, LEADER, state, None, callback(next_preview, "Пропустить"), settings
        )
        assert "Второй дополнительный вопрос" in final_preview.text
        for stale in (stale_answer, stale_exit):
            with pytest.raises(ValidationError, match="устар"):
                await handle_surveys(None, LEADER, state, None, stale, settings)

    asyncio.run(scenario())
