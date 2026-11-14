"""Private aggregate reports, calculated from raw ratings and historical cohorts."""

from calendar import monthrange
from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from cu_clubs.attendance_models import Attendance
from cu_clubs.event_models import Event, EventClub, RegistrationHistory
from cu_clubs.events import EventService
from cu_clubs.periods import academic_periods
from cu_clubs.services import ClubService, PermissionDenied, ValidationError
from cu_clubs.survey_models import SurveyAnswer, SurveyChoice, SurveyQuestion, SurveyResponse

MOSCOW = ZoneInfo("Europe/Moscow")


def reporting_range(name, *, today=None, starts_on=None, ends_before=None):
    """All ranges use local meeting start dates and an exclusive upper bound."""
    today = today or datetime.now(MOSCOW).date()
    academic_year = today.year if today.month >= 9 else today.year - 1
    current = academic_periods(academic_year)[0 if today.month >= 9 or today.month == 1 else 1]
    if today.month in {7, 8}:
        current = academic_periods(academic_year)[2]
    if name == "all":
        return None, None
    if name == "custom":
        if (
            not isinstance(starts_on, date)
            or not isinstance(ends_before, date)
            or starts_on >= ends_before
        ):
            raise ValidationError("Укажи начало и окончание периода; конец не включается.")
        return starts_on, ends_before
    if name == "semester_to_date":
        return current.starts_on, today + timedelta(days=1)
    if name == "current_semester":
        return current.starts_on, current.ends_before
    if name == "previous_semester":
        previous = (
            academic_periods(academic_year)[0]
            if today.month in range(2, 9)
            else academic_periods(academic_year - 1)[1]
        )
        return previous.starts_on, previous.ends_before
    if name in {"last_month", "year"}:
        month, year = (
            (today.month - 1, today.year) if name == "last_month" else (today.month, today.year - 1)
        )
        if month == 0:
            month, year = 12, year - 1
        return date(year, month, min(today.day, monthrange(year, month)[1])), today + timedelta(
            days=1
        )
    raise ValidationError("Неизвестный отчётный период.")


class AnalyticsService:
    def __init__(self, session):
        self.session = session
        self.clubs = ClubService(session)
        self.events = EventService(session)

    async def _club_access(self, actor, public_id):
        rights = await self.clubs.permissions(actor, public_id)
        if not (rights["is_admin"] or rights["is_leader"]):
            raise PermissionDenied("Аналитика доступна лидеру клуба и администратору.")
        return await self.clubs._club(public_id)

    async def event(self, actor, public_id):
        event = await self.events._event(public_id)
        await self.events._manage(actor, event, leaders_only=True)
        if (
            event.status not in {"held", "published"}
            or event.ends_at is None
            or event.ends_at > datetime.now(UTC)
        ):
            raise ValidationError("Аналитика доступна после окончания встречи.")
        result = await self._report([event])
        return {
            **result,
            "event": event.public_id,
            "title": event.title,
            "feedback": await self._feedback([event.id]),
        }

    async def club(self, actor, public_id, starts_on=None, ends_before=None, *, period=None):
        club = await self._club_access(actor, public_id)
        if period is not None:
            starts_on, ends_before = reporting_range(
                period, starts_on=starts_on, ends_before=ends_before
            )
        events = await self._events(club.id, starts_on, ends_before)
        return {
            **await self._report(events),
            "club": club.public_id,
            "name": club.name,
            "feedback": await self._feedback([e.id for e in events]),
        }

    async def university(self, actor, starts_on=None, ends_before=None, *, period=None):
        if not await self.clubs.is_admin(actor):
            raise PermissionDenied("Сводная аналитика доступна администратору.")
        if period is not None:
            starts_on, ends_before = reporting_range(
                period, starts_on=starts_on, ends_before=ends_before
            )
        return await self._report(await self._events(None, starts_on, ends_before))

    async def _events(self, club_id, starts_on, ends_before):
        if starts_on is not None and ends_before is not None and starts_on >= ends_before:
            raise ValidationError("Начало периода должно быть раньше окончания.")
        query = select(Event).where(
            Event.status.in_(["held", "published"]), Event.ends_at <= datetime.now(UTC)
        )
        if club_id:
            query = query.where(
                Event.id.in_(select(EventClub.event_id).where(EventClub.club_id == club_id))
            )
        if starts_on is not None:
            query = query.where(
                Event.starts_at >= datetime.combine(starts_on, datetime.min.time(), MOSCOW)
            )
        if ends_before is not None:
            query = query.where(
                Event.starts_at < datetime.combine(ends_before, datetime.min.time(), MOSCOW)
            )
        return list(await self.session.scalars(query.order_by(Event.starts_at)))

    async def _cohort(self, event):
        boundary = event.snapshot_starts_at or event.starts_at
        history = (
            list(
                await self.session.scalars(
                    select(RegistrationHistory)
                    .where(
                        RegistrationHistory.event_id == event.id,
                        RegistrationHistory.changed_at <= boundary,
                    )
                    .order_by(RegistrationHistory.changed_at, RegistrationHistory.id)
                )
            )
            if boundary
            else []
        )
        latest = {}
        for row in history:
            latest[str(row.person_id)] = row.status
        cohort = (
            set(event.start_registration_snapshot)
            if event.start_registration_snapshot is not None
            else {person for person, status in latest.items() if status == "active"}
        )
        cancelled = {person for person, status in latest.items() if status == "cancelled"}
        return cohort, cancelled

    async def _report(self, events):
        ids = [event.id for event in events]
        attendance = (
            list(await self.session.scalars(select(Attendance).where(Attendance.event_id.in_(ids))))
            if ids
            else []
        )
        responses = (
            list(
                await self.session.scalars(
                    select(SurveyResponse).where(
                        SurveyResponse.event_id.in_(ids), SurveyResponse.rating.is_not(None)
                    )
                )
            )
            if ids
            else []
        )
        registered, came_registered, cancelled = 0, 0, 0
        for event in events:
            cohort, cancellations = await self._cohort(event)
            visited = {str(a.person_id) for a in attendance if a.event_id == event.id}
            registered += len(cohort)
            came_registered += len(visited & cohort)
            cancelled += len(cancellations)
        ratings = [r.rating for r in responses]
        total = len(attendance)
        return {
            "events": len(events),
            "visits": total,
            "unique_visitors": len({a.person_id for a in attendance}),
            "registrations_at_start": registered,
            "attended_registered": came_registered,
            "attended_without_registration": total - came_registered,
            "cancelled_before_start": cancelled,
            "attendance_conversion": came_registered / registered if registered else None,
            "rating_sum": sum(ratings),
            "rating_count": len(ratings),
            "mean_rating": sum(ratings) / len(ratings) if ratings else None,
            "feedback_rate": len(ratings) / total if total else None,
        }

    async def _feedback(self, event_ids):
        """Return feedback only after private authorization, without respondent identities."""
        if not event_ids:
            return []
        rows = (
            await self.session.execute(
                select(SurveyQuestion, SurveyAnswer)
                .join(SurveyAnswer, SurveyAnswer.question_id == SurveyQuestion.id)
                .join(SurveyResponse, SurveyResponse.id == SurveyAnswer.response_id)
                .where(SurveyResponse.event_id.in_(event_ids), SurveyQuestion.position > 0)
                .order_by(SurveyQuestion.survey_version_id, SurveyQuestion.position)
            )
        ).all()
        grouped = {}
        for question, answer in rows:
            entry = grouped.setdefault(
                str(question.id),
                {
                    "question_id": str(question.id),
                    "version_id": str(question.survey_version_id),
                    "prompt": question.prompt,
                    "kind": question.kind,
                    "answers": [],
                    "distribution": {},
                    "skipped": 0,
                },
            )
            if answer.value is None:
                entry["skipped"] += 1
            elif question.kind == "text":
                entry["answers"].append(answer.value)
            else:
                values = answer.value if question.kind == "multiple" else [answer.value]
                for value in values:
                    label = str(value)
                    if question.kind in {"single", "multiple"}:
                        choice = await self.session.get(SurveyChoice, UUID(str(value)))
                        label = choice.label if choice else label
                    entry["distribution"][label] = entry["distribution"].get(label, 0) + 1
        return list(grouped.values())
