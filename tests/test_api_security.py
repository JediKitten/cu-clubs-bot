"""Independent integration-security acceptance on disposable PostgreSQL, offline HTTP."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select
from test_events import ADMIN, LEADER, OTHER, STUDENT, fields, published, seed
from test_events import sessions as sessions
from test_runtime import run, settings_for
from test_runtime import runtime_database_url as runtime_database_url

from cu_clubs.api import install_api
from cu_clubs.api_models import ApiGrant, ApiIdempotency, ApiLink
from cu_clubs.api_security import ApiError, digest
from cu_clubs.api_service import SCOPES, ApiService
from cu_clubs.app import create_app
from cu_clubs.attendance_models import Attendance
from cu_clubs.event_models import Event, Registration
from cu_clubs.events import EventService
from cu_clubs.models import Person
from cu_clubs.services import ClubService, PermissionDenied, ValidationError

SECRET = "OFFLINE_API_REVIEW_SECRET_32_CHARACTERS"


def api_settings():
    return SimpleNamespace(
        verification_secret=SecretStr(SECRET),
        telegram_bot_username="offline_test_bot",
        api_grant_ttl_days=30,
        api_rate_limit_per_minute=1000,
        api_enabled=True,
    )


def http_app(database_url):
    config = settings_for(database_url, verification_secret=SecretStr(SECRET)).model_copy(
        update={"api_enabled": True, "api_grant_ttl_days": 30, "api_rate_limit_per_minute": 1000}
    )
    app = create_app(config)
    if "/api/v1/events" not in app.openapi().get("paths", {}):
        install_api(app, config)
    return app


async def consent(service, client, actor):
    link = await service.create_link(client, actor)
    await service.approve_link(actor, link["id"])
    result = await service.exchange(client, link["id"], link["exchange_code"])
    await service.session.flush()
    return result["user_token"], link


def test_http_club_boundary_raw_ids_idempotency_and_ifmatch(runtime_database_url):
    prepared = {}

    async def setup(sessions, _):
        await seed(sessions)
        async with sessions.begin() as session:
            api, events = ApiService(session, api_settings()), EventService(session)
            prepared["key"] = (
                await api.issue_client(ADMIN, "cinema", "Offline full client", list(SCOPES))
            )["api_key"]
            foreign = await events.create_draft(ADMIN, "p" * 32, fields(visibility="closed"))
            prepared["foreign"] = (
                await events.publish(ADMIN, foreign["public_id"], foreign["version"])
            )["public_id"]
            own = await events.create_draft(LEADER, "cinema", fields())
            prepared["own"], prepared["version"] = own["public_id"], own["version"]

    run(runtime_database_url, setup)
    headers = {
        "Authorization": "Bearer " + prepared["key"],
        "Idempotency-Key": "offline-operation",
        "If-Match": '"1"',
    }
    with TestClient(http_app(runtime_database_url)) as client:
        foreign = prepared["foreign"]
        for method, path, body in [
            ("GET", f"/events/{foreign}", None),
            ("GET", f"/events/{foreign}/participants", None),
            ("PATCH", f"/events/{foreign}", {"title": "Changed foreign event"}),
            (
                "POST",
                f"/events/{foreign}/attendance",
                {"full_name": "Synthetic Visitor", "time_login": "synthetic.visitor"},
            ),
        ]:
            response = client.request(method, "/api/v1" + path, headers=headers, json=body)
            assert response.status_code == 404, response.text
            assert "Synthetic Visitor" not in response.text
        path = "/api/v1/events/" + prepared["own"]
        stale = client.patch(
            path, headers={**headers, "If-Match": '"999"'}, json={"title": "Fresh title"}
        )
        assert stale.status_code == 412
        first = client.patch(path, headers=headers, json={"title": "Fresh title"})
        assert first.status_code == 200, first.text
        replay = client.patch(path, headers=headers, json={"title": "Fresh title"})
        assert replay.status_code == 200 and replay.json() == first.json()
        assert replay.headers["Idempotency-Replayed"] == "true"
        changed = client.patch(path, headers=headers, json={"title": "Different title"})
        assert (
            changed.status_code == 409 and changed.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        )
        feed = client.get("/api/v1/changes", headers=headers)
        assert feed.status_code == 200
        assert all(item["event_id"] != foreign for item in feed.json()["items"])
        raw = client.put(
            path + "/registrations/me", headers={**headers, "X-User-Token": str(STUDENT)}, json={}
        )
        assert raw.status_code == 401
        spoof = client.put(
            path + "/registrations/me", headers=headers, json={"telegram_id": STUDENT}
        )
        assert spoof.status_code == 422

    async def verify(sessions, _):
        async with sessions() as session:
            event = await session.scalar(select(Event).where(Event.public_id == prepared["own"]))
            assert event.title == "Fresh title"
            assert await session.scalar(select(func.count()).select_from(Registration)) == 0
            assert await session.scalar(select(func.count()).select_from(Attendance)) == 0
            foreign = await session.scalar(
                select(Event).where(Event.public_id == prepared["foreign"])
            )
            assert foreign.title == "Встреча киноклуба"
            stored = list(await session.scalars(select(ApiIdempotency)))
            assert len(stored) == 1
            assert all(
                SECRET not in row.response_encrypted and "Fresh title" not in row.response_encrypted
                for row in stored
            )

    run(runtime_database_url, verify)


def test_event_read_key_does_not_impersonate_issuer(runtime_database_url):
    prepared = {}

    async def setup(sessions, _):
        await seed(sessions)
        event = await published(sessions)
        async with sessions.begin() as session:
            account = await ClubService(session)._account(LEADER)
            person = await session.get(Person, account.person_id)
            person.kind, person.verified_at, person.time_login = (
                "student",
                datetime.now(UTC),
                "synthetic.leader",
            )
            await EventService(session).register(LEADER, event["public_id"])
            prepared["event"] = event["public_id"]
            prepared["key"] = (
                await ApiService(session, api_settings()).issue_client(
                    LEADER, "cinema", "Read only client", ["events:read"]
                )
            )["api_key"]

    run(runtime_database_url, setup)
    with TestClient(http_app(runtime_database_url)) as client:
        result = client.get(
            "/api/v1/events/" + prepared["event"],
            headers={"Authorization": "Bearer " + prepared["key"]},
        )
        assert result.status_code == 200
        dto = result.json()
        assert dto.get("registered") in (None, False), (
            "A club key exposed the issuer's personal registration"
        )
        assert dto.get("registered_at") is None
        assert "post_registration_text" not in dto
        assert "synthetic.leader" not in result.text
        denied = client.patch(
            "/api/v1/events/" + prepared["event"],
            headers={
                "Authorization": "Bearer " + prepared["key"],
                "Idempotency-Key": "read-key-write",
                "If-Match": '"1"',
            },
            json={"title": "Forbidden change"},
        )
        assert denied.status_code == 403


def test_links_client_binding_one_use_grant_expiry_revocation(sessions):
    async def scenario():
        await seed(sessions)
        async with sessions.begin() as session:
            api = ApiService(session, api_settings())
            first = await api.issue_client(LEADER, "cinema", "Client one", list(SCOPES))
            second = await api.issue_client(LEADER, "cinema", "Client two", list(SCOPES))
            one, two = (
                await api.authenticate(first["api_key"]),
                await api.authenticate(second["api_key"]),
            )
            pending = await api.create_link(one, STUDENT)
            with pytest.raises(ApiError) as error:
                await api.exchange(one, pending["id"], pending["exchange_code"])
            assert error.value.code == "LINK_PENDING"
            raw = pending["verification_uri"].split("start=l_")[1]
            with pytest.raises(ValidationError):
                await api.open_link(OTHER, raw)
            with pytest.raises(ApiError) as error:
                await api.exchange(two, pending["id"], pending["exchange_code"])
            assert error.value.status == 404
            await api.approve_link(STUDENT, pending["id"])
            grant = await api.exchange(one, pending["id"], pending["exchange_code"])
            token = grant["user_token"]
            await session.flush()
            actor, row = await api.delegated_actor(one, token)
            assert actor == STUDENT
            with pytest.raises(ApiError):
                await api.delegated_actor(two, token)
            with pytest.raises(ApiError) as error:
                await api.exchange(one, pending["id"], pending["exchange_code"])
            assert error.value.code == "LINK_EXPIRED_OR_USED"
            with pytest.raises(PermissionDenied):
                await api.revoke_grant(OTHER, row.id)
            await api.revoke_grant(STUDENT, row.id)
            await session.flush()
            with pytest.raises(ApiError):
                await api.delegated_actor(one, token)
            fresh, _ = await consent(api, one, STUDENT)
            fresh_row = await session.scalar(
                select(ApiGrant).where(ApiGrant.token_hash == digest(fresh))
            )
            fresh_row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.flush()
            with pytest.raises(ApiError):
                await api.delegated_actor(one, fresh)
            expired = await api.create_link(one, STUDENT)
            expired_row = await session.get(ApiLink, UUID(expired["id"]))
            expired_row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            with pytest.raises(ValidationError):
                await api.approve_link(STUDENT, expired["id"])
            await api.revoke_client(LEADER, first["id"])
            with pytest.raises(ApiError) as error:
                await api.authenticate(first["api_key"])
            assert error.value.status == 401

    asyncio.run(scenario())


def test_api_domain_and_telegram_race_for_last_seat(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions, capacity=1)
        async with sessions.begin() as session:
            api = ApiService(session, api_settings())
            issued = await api.issue_client(LEADER, "cinema", "Booking client", list(SCOPES))
            owner = await api.authenticate(issued["api_key"])
            token, _ = await consent(api, owner, STUDENT)

        async def api_register():
            try:
                async with sessions.begin() as session:
                    api = ApiService(session, api_settings())
                    client = await api.authenticate(issued["api_key"], "registration:self")
                    actor, _ = await api.delegated_actor(client, token)
                    await api.event_access(client, event["public_id"], lock=True)
                    await EventService(session).register(
                        actor, event["public_id"], source="api", via_link=True
                    )
                return True
            except ValidationError:
                return False

        async def telegram_register():
            try:
                async with sessions.begin() as session:
                    await EventService(session).register(OTHER, event["public_id"])
                return True
            except ValidationError:
                return False

        assert sum(await asyncio.gather(api_register(), telegram_register())) == 1
        async with sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Registration)
                    .where(Registration.status == "active")
                )
                == 1
            )

    asyncio.run(scenario())


def test_delegation_cannot_expose_another_clubs_personal_event_data(runtime_database_url):
    prepared = {}

    async def setup(sessions, _):
        await seed(sessions)
        async with sessions.begin() as session:
            api, events = ApiService(session, api_settings()), EventService(session)
            issued = await api.issue_client(
                ADMIN, "cinema", "Bound delegation client", list(SCOPES)
            )
            client = await api.authenticate(issued["api_key"])
            token, _ = await consent(api, client, STUDENT)
            foreign = await events.create_draft(
                ADMIN,
                "p" * 32,
                fields(
                    visibility="open", post_registration_text="FOREIGN_PRIVATE_REGISTERED_SECRET"
                ),
            )
            foreign = await events.publish(ADMIN, foreign["public_id"], foreign["version"])
            await events.register(STUDENT, foreign["public_id"])
            prepared.update(event=foreign["public_id"], key=issued["api_key"], token=token)

    run(runtime_database_url, setup)
    headers = {
        "Authorization": "Bearer " + prepared["key"],
        "X-User-Token": prepared["token"],
        "Idempotency-Key": "foreign-personal-operation",
    }
    path = "/api/v1/events/" + prepared["event"]
    with TestClient(http_app(runtime_database_url)) as client:
        result = client.get(path, headers=headers)
        assert result.status_code in (200, 404)
        assert "FOREIGN_PRIVATE_REGISTERED_SECRET" not in result.text
        if result.status_code == 200:
            assert result.json().get("registered") in (None, False)
            assert result.json().get("registered_at") is None
            assert "post_registration_text" not in result.json()
        for suffix in ("/participants", "/participants/me"):
            assert client.get(path + suffix, headers=headers).status_code == 404
        assert client.put(path + "/registrations/me", headers=headers, json={}).status_code == 404


def test_http_single_use_exchange_has_encrypted_idempotent_replay(runtime_database_url):
    prepared = {}

    async def setup(sessions, _):
        await seed(sessions)
        async with sessions.begin() as session:
            prepared["key"] = (
                await ApiService(session, api_settings()).issue_client(
                    LEADER, "cinema", "Exchange replay client", ["registration:self"]
                )
            )["api_key"]

    run(runtime_database_url, setup)
    headers = {"Authorization": "Bearer " + prepared["key"]}
    with TestClient(http_app(runtime_database_url)) as client:
        link = client.post(
            "/api/v1/links",
            headers={**headers, "Idempotency-Key": "start-link"},
            json={"telegram_id": STUDENT},
        )
        assert link.status_code == 201, link.text
        item = link.json()

        async def approve(sessions, _):
            async with sessions.begin() as session:
                await ApiService(session, api_settings()).approve_link(STUDENT, item["id"])

        run(runtime_database_url, approve)
        path, body = (
            "/api/v1/links/" + item["id"] + "/exchange",
            {"exchange_code": item["exchange_code"]},
        )
        first = client.post(
            path, headers={**headers, "Idempotency-Key": "exchange-link"}, json=body
        )
        assert first.status_code == 200, first.text
        token = first.json()["user_token"]
        replay = client.post(
            path, headers={**headers, "Idempotency-Key": "exchange-link"}, json=body
        )
        assert replay.status_code == 200 and replay.json() == first.json()
        assert replay.headers["Idempotency-Replayed"] == "true"
        reused = client.post(
            path, headers={**headers, "Idempotency-Key": "another-exchange"}, json=body
        )
        assert reused.status_code == 409
        linked = client.get("/api/v1/me", headers={**headers, "X-User-Token": token})
        assert linked.status_code == 200 and linked.json()["time_login"] == f"u{STUDENT}"

        async def revoke_and_inspect(sessions, _):
            async with sessions.begin() as session:
                grants = list(await session.scalars(select(ApiGrant)))
                assert len(grants) == 1 and grants[0].token_hash == digest(token)
                row = await session.get(ApiLink, UUID(item["id"]))
                assert row.exchange_hash == digest(item["exchange_code"])
                assert row.approve_hash != item["verification_uri"].split("start=l_")[1]
                for record in await session.scalars(select(ApiIdempotency)):
                    assert token not in record.response_encrypted
                    assert item["exchange_code"] not in record.response_encrypted
                    assert prepared["key"] not in record.response_encrypted
                await ApiService(session, api_settings()).revoke_grant(STUDENT, grants[0].id)

        run(runtime_database_url, revoke_and_inspect)
        revoked = client.get("/api/v1/me", headers={**headers, "X-User-Token": token})
        assert revoked.status_code == 401
