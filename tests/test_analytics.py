"""Weighted reports, collaboration deduplication and historical conversion."""

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from test_events import ADMIN, EDITOR, LEADER, OTHER, STUDENT, published, seed
from test_events import sessions as sessions
from test_surveys import visit

from cu_clubs.analytics import AnalyticsService, reporting_range
from cu_clubs.event_models import EventClub, RegistrationHistory
from cu_clubs.models import Club
from cu_clubs.services import ClubService, PermissionDenied
from cu_clubs.surveys import SurveysService


def test_reporting_periods():
    assert reporting_range("current_semester", today=date(2027, 6, 10)) == (
        date(2027, 2, 1),
        date(2027, 7, 1),
    )
    assert reporting_range("previous_semester", today=date(2027, 1, 10)) == (
        date(2026, 2, 1),
        date(2026, 7, 1),
    )
    assert reporting_range("last_month", today=date(2027, 3, 31))[0] == date(2027, 2, 28)


def test_weighted_raw_ratings_historical_cohort_collaboration(sessions):
    async def scenario():
        await seed(sessions)
        first = await published(sessions)
        second = await published(sessions)
        async with sessions.begin() as session:
            surveys, reports = SurveysService(session), AnalyticsService(session)
            first_event, student = await visit(session, first["public_id"], STUDENT)
            second_event, _ = await visit(session, second["public_id"], STUDENT)
            await visit(session, second["public_id"], OTHER)
            await visit(session, second["public_id"], EDITOR)
            partner = await session.scalar(select(Club).where(Club.public_id == "p" * 32))
            session.add(EventClub(event_id=second_event.id, club_id=partner.id, role="partner"))
            first_event.start_registration_snapshot = [str(student)]
            first_event.snapshot_starts_at = first_event.starts_at
            second_event.start_registration_snapshot = [str(student)]
            second_event.snapshot_starts_at = second_event.starts_at
            # A cancellation after the start must not rewrite the stored denominator.
            session.add(
                RegistrationHistory(
                    event_id=first_event.id,
                    person_id=student,
                    status="cancelled",
                    changed_at=datetime.now(UTC),
                )
            )
            other_person = (await ClubService(session)._account(OTHER)).person_id
            session.add(
                RegistrationHistory(
                    event_id=second_event.id,
                    person_id=other_person,
                    status="cancelled",
                    changed_at=second_event.starts_at - timedelta(minutes=1),
                )
            )
            for event, actor, score in [
                (first, STUDENT, 5),
                (second, STUDENT, 3),
                (second, OTHER, 3),
                (second, EDITOR, 3),
            ]:
                response = await surveys.start_response(actor, event["public_id"])
                await surveys.answer(
                    actor, event["public_id"], response["next_question"]["id"], score
                )
            report = await reports.club(LEADER, "cinema")
            assert report["mean_rating"] == 3.5
            assert report["rating_count"] == 4 and report["rating_sum"] == 14
            assert (
                report["events"] == 2 and report["visits"] == 4 and report["unique_visitors"] == 3
            )
            assert report["registrations_at_start"] == 2 and report["attended_registered"] == 2
            assert (
                report["attendance_conversion"] == 1
                and report["attended_without_registration"] == 2
            )
            assert report["cancelled_before_start"] == 1
            overall = await reports.university(ADMIN)
            assert overall["events"] == 2 and overall["rating_count"] == 4
            assert "feedback" not in overall and "person_id" not in str(overall)
            with pytest.raises(PermissionDenied):
                await reports.club(EDITOR, "cinema")
            with pytest.raises(PermissionDenied):
                await reports.university(LEADER)
            empty = await reports.club(LEADER, "cinema", date(2000, 1, 1), date(2001, 1, 1))
            assert empty["mean_rating"] is None and empty["attendance_conversion"] is None

    asyncio.run(scenario())
