"""PostgreSQL acceptance checks for public club-card additions."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from test_clubs import LEADER, run, seed

from cu_clubs.club_card_ui import club_card_extras
from cu_clubs.event_models import Event, EventClub
from cu_clubs.models import Club
from cu_clubs.services import ClubService, ValidationError

pytest_plugins = ["test_clubs"]

pytestmark = pytest.mark.integration


def test_card_extras_include_direction_and_only_three_future_open_published(database_url):
    async def scenario(sessions):
        await seed(sessions)
        now = datetime(2026, 11, 14, 12, tzinfo=UTC)
        async with sessions.begin() as session:
            service = ClubService(session)
            club = await service.get_club("cinema")
            club = await service.update_club(LEADER, "cinema", "direction", "Hard Skill Hub")
            club_row = await session.scalar(select(Club).where(Club.public_id == "cinema"))
            cases = [
                ("published", "open", 1, True),
                ("published", "open", 2, True),
                ("published", "open", 3, True),
                ("published", "open", 4, True),
                ("draft", "open", 0, True),
                ("pending_partner", "open", 0, True),
                ("cancelled", "open", 0, True),
                ("held", "open", 0, True),
                ("published", "closed", 0, True),
                ("published", "open", -1, True),
            ]
            for i, (status, visibility, hours, _) in enumerate(cases):
                event = Event(
                    initiator_club_id=club_row.id,
                    author_person_id=(await service._account(LEADER)).person_id,
                    last_editor_telegram_id=LEADER,
                    title=f"Event {i}",
                    status=status,
                    visibility=visibility,
                    starts_at=now + timedelta(hours=hours),
                )
                session.add(event)
                await session.flush()
                session.add(EventClub(event_id=event.id, club_id=club_row.id, role="initiator"))
            await session.flush()
            extras = await club_card_extras(session, club, bot_username="clubs_bot", now=now)
            assert "Hard Skill Hub" in extras["text"]
            assert [item["title"] for item in extras["events"]] == ["Event 0", "Event 1", "Event 2"]
            assert all(
                item["url"].startswith("https://t.me/clubs_bot?start=e_")
                for item in extras["events"]
            )
            assert extras["back_route"] == {"kind": "club_card", "public_id": "cinema"}

    run(database_url, scenario)


def test_direction_is_optional_and_limited_to_100_characters(database_url):
    async def scenario(sessions):
        await seed(sessions)
        async with sessions.begin() as session:
            service = ClubService(session)
            original = await service.get_club("cinema")
            assert original["direction"] is None
            updated = await service.update_club(LEADER, "cinema", "direction", "  Health Hub  ")
            assert updated["direction"] == "Health Hub"
            with pytest.raises(ValidationError):
                await service.update_club(LEADER, "cinema", "direction", "Gaming")
            with pytest.raises(ValidationError):
                await service.update_club(LEADER, "cinema", "direction", "x" * 101)
            cleared = await service.update_club(LEADER, "cinema", "direction", "")
            assert cleared["direction"] is None

    run(database_url, scenario)
