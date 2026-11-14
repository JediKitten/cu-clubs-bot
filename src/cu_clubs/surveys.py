"""Survey commands shared by Telegram/API; caller owns the transaction."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert

from cu_clubs.attendance_models import Attendance
from cu_clubs.event_models import Event
from cu_clubs.events import EventService
from cu_clubs.models import Audit, Club, Outbox, Person, TelegramAccount
from cu_clubs.services import ClubService, PermissionDenied, ValidationError
from cu_clubs.survey_models import (
    RATING_PROMPT,
    SurveyAnswer,
    SurveyChoice,
    SurveyQuestion,
    SurveyResponse,
    SurveyVersion,
)


def utcnow():
    return datetime.now(UTC)


def first_question():
    return {
        "kind": "rating",
        "prompt": RATING_PROMPT,
        "required": True,
        "scale_max": 5,
        "choices": [],
    }


def validate_questions(questions):
    """Input may contain all questions or just additions; rating is never editable."""
    if not isinstance(questions, list) or len(questions) > 50:
        raise ValidationError("Допускается не больше 50 вопросов.")
    questions = list(questions)
    if questions and isinstance(questions[0], dict) and questions[0].get("kind") == "rating":
        given = questions.pop(0)
        if any(
            given.get(k) != v for k, v in first_question().items() if k != "choices"
        ) or given.get("choices", []):
            raise ValidationError("Первый вопрос и шкалу 1–5 нельзя изменить.")
    result = [first_question()]
    for q in questions:
        if not isinstance(q, dict) or q.keys() - {
            "id",
            "position",
            "kind",
            "prompt",
            "required",
            "scale_max",
            "choices",
        }:
            raise ValidationError("Неизвестное поле вопроса.")
        kind, prompt, required = q.get("kind"), q.get("prompt"), q.get("required", False)
        if kind not in {"text", "single", "multiple", "scale"}:
            raise ValidationError("Выбери один из четырёх типов дополнительного вопроса.")
        if (
            not isinstance(prompt, str)
            or not 1 <= len(prompt.strip()) <= 1000
            or not isinstance(required, bool)
        ):
            raise ValidationError("Укажи вопрос до 1000 символов и обязательность ответа.")
        choices, maximum = q.get("choices", []), q.get("scale_max")
        if kind in {"single", "multiple"}:
            if not isinstance(choices, list):
                raise ValidationError("Укажи варианты ответа.")
            choices = [item.get("label") if isinstance(item, dict) else item for item in choices]
            if not 2 <= len(choices) <= 30 or any(
                not isinstance(v, str) or not 1 <= len(v.strip()) <= 300 for v in choices
            ):
                raise ValidationError("Нужно от 2 до 30 вариантов до 300 символов.")
            choices = [v.strip() for v in choices]
            if len(set(choices)) != len(choices):
                raise ValidationError("Варианты не должны повторяться.")
        elif choices:
            raise ValidationError("Для этого типа варианты не нужны.")
        if kind == "scale":
            if (
                isinstance(maximum, bool)
                or not isinstance(maximum, int)
                or not 2 <= maximum < 2**31
            ):
                raise ValidationError("Верхняя граница шкалы должна быть целым числом N ≥ 2.")
        elif maximum is not None:
            raise ValidationError("Шкала задаётся только для числового вопроса.")
        result.append(
            {
                "kind": kind,
                "prompt": prompt.strip(),
                "required": required,
                "scale_max": maximum,
                "choices": choices,
            }
        )
    return result


class SurveysService:
    def __init__(self, session):
        self.session = session
        self.clubs = ClubService(session)
        self.events = EventService(session)

    async def _latest(self, *, club_id=None, event_id=None):
        condition = (
            SurveyVersion.club_id == club_id if club_id else SurveyVersion.event_id == event_id
        )
        return await self.session.scalar(
            select(SurveyVersion).where(condition).order_by(SurveyVersion.version.desc()).limit(1)
        )

    async def _write(self, questions, previous=None, *, club_id=None, event_id=None):
        checked = validate_questions(questions)
        version = SurveyVersion(
            club_id=club_id, event_id=event_id, version=previous.version + 1 if previous else 1
        )
        self.session.add(version)
        await self.session.flush()
        for position, item in enumerate(checked):
            question = SurveyQuestion(
                survey_version_id=version.id,
                position=position,
                kind=item["kind"],
                prompt=item["prompt"],
                required=item["required"],
                scale_max=item["scale_max"],
            )
            self.session.add(question)
            await self.session.flush()
            for index, label in enumerate(item["choices"]):
                self.session.add(SurveyChoice(question_id=question.id, position=index, label=label))
        await self.session.flush()
        return version

    async def _view(self, version):
        questions = list(
            await self.session.scalars(
                select(SurveyQuestion)
                .where(SurveyQuestion.survey_version_id == version.id)
                .order_by(SurveyQuestion.position)
            )
        )
        choices = list(
            await self.session.scalars(
                select(SurveyChoice)
                .where(SurveyChoice.question_id.in_([q.id for q in questions]))
                .order_by(SurveyChoice.position)
            )
        )
        return {
            "id": str(version.id),
            "version": version.version,
            "questions": [
                {
                    "id": str(q.id),
                    "position": q.position,
                    "kind": q.kind,
                    "prompt": q.prompt,
                    "required": q.required,
                    "scale_max": q.scale_max,
                    "choices": [
                        {"id": str(c.id), "label": c.label}
                        for c in choices
                        if c.question_id == q.id
                    ],
                }
                for q in questions
            ],
        }

    async def _club(self, actor, public_id):
        club = await self.clubs._club(public_id)
        rights = await self.clubs.permissions(actor, public_id)
        if not (rights["is_admin"] or rights["is_leader"]):
            raise PermissionDenied("Опрос доступен лидеру клуба и администратору.")
        return await self.session.scalar(select(Club).where(Club.id == club.id).with_for_update())

    async def get_club(self, actor, club):
        owner = await self._club(actor, club)
        version = await self._latest(club_id=owner.id)
        if version is None:
            version = await self._write([], club_id=owner.id)
        return await self._view(version)

    async def save_club(self, actor, club, questions, expected_version=None):
        owner = await self._club(actor, club)
        previous = await self._latest(club_id=owner.id)
        self._check_version(previous, expected_version)
        version = await self._write(questions, previous, club_id=owner.id)
        self.session.add(
            Audit(
                actor_telegram_id=actor,
                action="survey.club.saved",
                entity_id=owner.id,
                details={"version": version.version},
            )
        )
        return await self._view(version)

    @staticmethod
    def _check_version(previous, expected):
        if expected is not None and expected != (previous.version if previous else 0):
            raise ValidationError("Анкета уже изменилась. Открой актуальную версию.")

    async def ensure_event_snapshot(self, event):
        """Call while creating/publishing an event; copy the initiating club once."""
        # Event lock serializes creation with edits, responses and invitation ticks.
        await self.session.scalar(select(Event.id).where(Event.id == event.id).with_for_update())
        current = await self._latest(event_id=event.id)
        if current is not None:
            return current
        club = await self._latest(club_id=event.initiator_club_id)
        questions = (await self._view(club))["questions"] if club else []
        return await self._write(questions, event_id=event.id)

    async def snapshot_event(self, event_id):
        event = await self.session.get(Event, UUID(str(event_id)))
        if event is None:
            raise ValidationError("Встреча не найдена.")
        return await self._view(await self.ensure_event_snapshot(event))

    async def get_event(self, actor, event):
        owner = await self.events._event(event, lock=True)
        await self.events._manage(actor, owner, leaders_only=True)
        return await self._view(await self.ensure_event_snapshot(owner))

    async def save_event(self, actor, event, questions, expected_version=None):
        owner = await self.events._event(event, lock=True)
        await self.events._manage(actor, owner, leaders_only=True)
        previous = await self.ensure_event_snapshot(owner)
        self._check_version(previous, expected_version)
        version = await self._write(questions, previous, event_id=owner.id)
        self.session.add(
            Audit(
                actor_telegram_id=actor,
                action="survey.event.saved",
                entity_id=owner.id,
                details={"version": version.version},
            )
        )
        return await self._view(version)

    async def _eligible(self, actor, public_id):
        event = await self.events._event(public_id, lock=True)
        if (
            event.status not in {"published", "held"}
            or event.ends_at is None
            or event.ends_at > utcnow()
        ):
            raise ValidationError("Опрос доступен после окончания встречи.")
        account = await self.clubs._account(actor)
        if (
            account is None
            or await self.session.get(Attendance, (event.id, account.person_id)) is None
        ):
            raise PermissionDenied("Опрос доступен только посетившим встречу.")
        return event, account.person_id

    async def start_response(self, actor, event):
        owner, person_id = await self._eligible(actor, event)
        response = await self.session.scalar(
            select(SurveyResponse).where(
                SurveyResponse.event_id == owner.id, SurveyResponse.person_id == person_id
            )
        )
        if response is None:
            version = await self.ensure_event_snapshot(owner)
            response = SurveyResponse(
                event_id=owner.id, person_id=person_id, survey_version_id=version.id
            )
            self.session.add(response)
            await self.session.flush()
        return await self._response_view(owner, response)

    async def _response_view(self, event, response):
        version = await self.session.get(SurveyVersion, response.survey_version_id)
        survey = await self._view(version)
        answers = list(
            await self.session.scalars(
                select(SurveyAnswer).where(SurveyAnswer.response_id == response.id)
            )
        )
        answered = {str(a.question_id): a.value for a in answers}
        next_question = next((q for q in survey["questions"] if q["id"] not in answered), None)
        return {
            **survey,
            "response_id": str(response.id),
            "event": event.public_id,
            "title": event.title,
            "rating": response.rating,
            "answers": answered,
            "next_question": next_question,
            "completed": response.completed_at is not None,
        }

    async def answer(self, actor, event, question_id, value):
        owner, person_id = await self._eligible(actor, event)
        response = await self.session.scalar(
            select(SurveyResponse)
            .where(SurveyResponse.event_id == owner.id, SurveyResponse.person_id == person_id)
            .with_for_update()
        )
        if response is None:
            raise ValidationError("Сначала открой анкету.")
        view = await self._response_view(owner, response)
        question = next((q for q in view["questions"] if q["id"] == str(question_id)), None)
        if question is None:
            raise ValidationError("Вопрос не принадлежит твоей версии анкеты.")
        # A stale callback is idempotent: never replace an already persisted rating/answer.
        if question["id"] in view["answers"]:
            return view
        if view["next_question"] is None or view["next_question"]["id"] != question["id"]:
            raise ValidationError("Ответь на текущий вопрос.")
        value = self._validate_answer(question, value)
        self.session.add(
            SurveyAnswer(response_id=response.id, question_id=UUID(question["id"]), value=value)
        )
        if question["position"] == 0:
            response.rating = value
        await self.session.flush()
        result = await self._response_view(owner, response)
        if result["next_question"] is None:
            response.completed_at = utcnow()
            result["completed"] = True
        return result

    @staticmethod
    def _validate_answer(question, value):
        if value is None:
            if question["required"]:
                raise ValidationError("На этот вопрос нужно ответить.")
            return None
        kind = question["kind"]
        if kind in {"rating", "scale"}:
            maximum = question["scale_max"]
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValidationError(f"Выбери значение от 1 до {maximum}")
        elif kind == "text":
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 4000:
                raise ValidationError("Ответ должен содержать от 1 до 4000 символов.")
            value = value.strip()
        else:
            allowed = {c["id"] for c in question["choices"]}
            if kind == "single" and (not isinstance(value, str) or value not in allowed):
                raise ValidationError("Выбери вариант кнопкой.")
            if kind == "multiple":
                if (
                    not isinstance(value, list)
                    or not value
                    or any(not isinstance(v, str) or v not in allowed for v in value)
                ):
                    raise ValidationError("Выбери хотя бы один вариант и нажми «Готово».")
                value = list(dict.fromkeys(value))
        return value

    async def pending(self, actor):
        account = await self.clubs._account(actor)
        if account is None:
            return []
        events = list(
            await self.session.scalars(
                select(Event)
                .join(Attendance, Attendance.event_id == Event.id)
                .where(
                    Attendance.person_id == account.person_id,
                    Event.status.in_(["published", "held"]),
                    Event.ends_at <= utcnow(),
                )
                .order_by(Event.starts_at.desc())
            )
        )
        complete = set(
            await self.session.scalars(
                select(SurveyResponse.event_id).where(
                    SurveyResponse.person_id == account.person_id,
                    SurveyResponse.completed_at.is_not(None),
                )
            )
        )
        return [{"event": e.public_id, "title": e.title} for e in events if e.id not in complete]

    async def enqueue_invitations(self, now=None):
        """Late attendance/profile linkage is discovered on each tick; unique outbox dedupes."""
        now = now or utcnow()
        rows = (
            await self.session.execute(
                select(Event, Attendance, TelegramAccount)
                .join(Attendance, Attendance.event_id == Event.id)
                .join(TelegramAccount, TelegramAccount.person_id == Attendance.person_id)
                .join(Person, Person.id == Attendance.person_id)
                .where(
                    Event.status.in_(["published", "held"]),
                    Event.ends_at <= now,
                    or_(Person.kind == "guest", Person.verified_at.is_not(None)),
                )
            )
        ).all()
        count = 0
        for event, attendance, account in rows:
            answered = await self.session.scalar(
                select(SurveyResponse.rating).where(
                    SurveyResponse.event_id == event.id,
                    SurveyResponse.person_id == attendance.person_id,
                )
            )
            if answered is not None:
                continue
            result = await self.session.execute(
                insert(Outbox)
                .values(
                    kind="survey.invitation",
                    recipient_telegram_id=account.telegram_id,
                    payload={"event": event.public_id, "title": event.title},
                    dedupe_key=f"survey:{event.id}:{attendance.person_id}",
                )
                .on_conflict_do_nothing(index_elements=[Outbox.dedupe_key])
                .returning(Outbox.id)
            )
            count += int(result.scalar_one_or_none() is not None)
        return count


async def merge_survey_people(session, old_id, new_id):
    """Move responses without losing a rating; reject ambiguous duplicate visits atomically."""
    if old_id == new_id:
        return
    # The identity service holds person locks; response writers lock their meeting.
    rows = list(
        await session.scalars(
            select(SurveyResponse)
            .where(SurveyResponse.person_id.in_([old_id, new_id]))
            .order_by(SurveyResponse.event_id, SurveyResponse.id)
            .with_for_update()
        )
    )
    old = [r for r in rows if r.person_id == old_id]
    new_events = {r.event_id for r in rows if r.person_id == new_id}
    if any(r.event_id in new_events for r in old):
        raise ValidationError(
            "У двух профилей есть ответы на один опрос. Объединение остановлено: "
            "требуется проверка администратора, обе оценки сохранены."
        )
    for response in old:
        response.person_id = new_id
    if old:
        session.add(
            Audit(
                action="survey.people.merged",
                entity_id=new_id,
                details={"source_person_id": str(old_id), "responses": len(old)},
            )
        )
        await session.flush()
