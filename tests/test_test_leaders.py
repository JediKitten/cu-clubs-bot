"""The temporary leader exception is explicit, test-only, and never verifies email."""

import asyncio

import pytest
from pydantic import ValidationError as SettingsError
from test_identity import ADMIN, STUDENT, seed
from test_identity import sessions as sessions

from cu_clubs.config import Settings
from cu_clubs.identity import AdminService
from cu_clubs.services import ClubService, ValidationError


def test_test_leader_flag_is_rejected_in_production():
    with pytest.raises(SettingsError):
        Settings(_env_file=None, environment="production", test_allow_unverified_leaders=True)


def test_known_username_is_exact_case_insensitive_and_leader_bypass_does_not_verify(sessions):
    async def run():
        await seed(sessions)
        async with sessions.begin() as session:
            service = ClubService(session)
            await service.get_or_create_person(STUDENT, "Тестовый Лидер", "test_leader")
            target = await service.resolve_telegram_target("@TEST_LEADER")
            assert target["telegram_id"] == STUDENT
            with pytest.raises(ValidationError):
                await service.resolve_telegram_target("@testXleader")
            with pytest.raises(ValidationError, match="почту"):
                await AdminService(session).create_club(ADMIN, "Строгий", "Seed", STUDENT)
            club = await AdminService(session, allow_unverified_leaders=True).create_club(
                ADMIN, "Тестовый клуб", "Seed", target["telegram_id"]
            )
            assert (await service.permissions(STUDENT, club["public_id"]))["is_leader"]
            account = await service._account(STUDENT)
            assert account.person.verified_at is None

    asyncio.run(run())
