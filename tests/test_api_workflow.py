"""Offline HTTP workflows and the independently runnable client/signature example."""

import importlib.util
import io
from pathlib import Path
from uuid import UUID

import httpx
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import func, select
from test_api_security import api_settings, consent, http_app
from test_events import LEADER, PARTNER, STUDENT, fields, published, seed
from test_runtime import run
from test_runtime import runtime_database_url as runtime_database_url

from cu_clubs.api_delivery import signed_headers
from cu_clubs.api_models import ApiDelivery
from cu_clubs.api_service import SCOPES, ApiService
from cu_clubs.attendance_models import Attendance, AttendanceImport
from cu_clubs.events import EventService
from cu_clubs.models import Person


def payload(**updates):
    result = fields(**updates)
    return {
        key: value.isoformat() if hasattr(value, "isoformat") else value
        for key, value in result.items()
    }


def prepare(database_url):
    data = {}

    async def setup(sessions, _):
        await seed(sessions)
        async with sessions.begin() as session:
            api = ApiService(session, api_settings())
            issued = await api.issue_client(LEADER, "cinema", "HTTP workflow client", list(SCOPES))
            client = await api.authenticate(issued["api_key"])
            token, _ = await consent(api, client, STUDENT)
            data.update(key=issued["api_key"], token=token)

    run(database_url, setup)
    return data


def test_http_create_edit_publish_invite_register_and_cancel(runtime_database_url):
    data = prepare(runtime_database_url)
    headers = {"Authorization": "Bearer " + data["key"]}
    with TestClient(http_app(runtime_database_url)) as client:
        created = client.post(
            "/api/v1/events",
            headers={**headers, "Idempotency-Key": "create-workflow"},
            json=payload(public_contact="@calendar_public_contact"),
        )
        assert created.status_code == 201, created.text
        draft = created.json()
        assert draft["public_contact"] == "@calendar_public_contact"
        path = "/api/v1/events/" + draft["id"]
        fetched = client.get(path, headers=headers)
        assert fetched.status_code == 200 and fetched.headers["ETag"] == f'"{draft["version"]}"'
        edited = client.patch(
            path,
            headers={
                **headers,
                "Idempotency-Key": "edit-workflow",
                "If-Match": fetched.headers["ETag"],
            },
            json={"description": "Updated workflow description"},
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["public_contact"] == "@calendar_public_contact"
        public = client.post(
            path + "/publish",
            headers={
                **headers,
                "Idempotency-Key": "publish-workflow",
                "If-Match": edited.headers["ETag"],
            },
        )
        assert public.status_code == 200 and public.json()["status"] == "published", public.text
        updated = client.patch(
            path,
            headers={
                **headers,
                "Idempotency-Key": "update-published",
                "If-Match": public.headers["ETag"],
            },
            json={"room": "Synthetic workflow room"},
        )
        assert updated.status_code == 200, updated.text
        user_headers = {**headers, "X-User-Token": data["token"]}
        booked = client.put(
            path + "/registrations/me", headers={**user_headers, "Idempotency-Key": "book-workflow"}
        )
        assert booked.status_code == 200 and booked.json()["registered"] is True, booked.text
        own = client.get(path + "/participants/me", headers=user_headers)
        assert own.status_code == 200 and own.json()["registration_status"] == "active"
        cancelled = client.delete(
            path + "/registrations/me",
            headers={**user_headers, "Idempotency-Key": "unbook-workflow"},
        )
        assert cancelled.status_code == 200 and cancelled.json()["registered"] is False
        cancelled_event = client.post(
            path + "/cancel",
            headers={
                **headers,
                "Idempotency-Key": "cancel-workflow",
                "If-Match": updated.headers["ETag"],
            },
        )
        assert (
            cancelled_event.status_code == 200 and cancelled_event.json()["status"] == "cancelled"
        )

        collaborative = client.post(
            "/api/v1/events",
            headers={**headers, "Idempotency-Key": "create-collab"},
            json=payload(title="Collaboration workflow"),
        )
        assert collaborative.status_code == 201
        collab_path = "/api/v1/events/" + collaborative.json()["id"]
        invitation = client.post(
            collab_path + "/invite",
            headers={
                **headers,
                "Idempotency-Key": "invite-workflow",
                "If-Match": collaborative.headers["ETag"],
            },
            json={"club_id": "p" * 32},
        )
        assert invitation.status_code == 200 and invitation.json()["status"] == "pending_partner", (
            invitation.text
        )

        async def approve(sessions, _):
            async with sessions.begin() as session:
                events = EventService(session)
                pending = await events.pending_invitations(PARTNER)
                item = next(
                    row for row in pending if row["public_id"] == collaborative.json()["id"]
                )
                await events.respond_invitation(PARTNER, item["invitation_id"], True)

        run(runtime_database_url, approve)
        accepted = client.get(collab_path, headers=headers)
        assert accepted.status_code == 200 and accepted.json()["status"] == "published"
        assert len(accepted.json()["clubs"]) == 2
        changes = client.get("/api/v1/changes", headers=headers)
        assert changes.status_code == 200
        kinds = {row["type"] for row in changes.json()["items"]}
        assert {
            "event.published",
            "event.updated",
            "event.cancelled",
            "registration.changed",
        } <= kinds


def test_http_multipart_preview_is_side_effect_free_and_confirm_once(runtime_database_url):
    data = prepare(runtime_database_url)
    state = {}

    async def event_setup(sessions, _):
        state["event"] = (await published(sessions))["public_id"]
        async with sessions() as session:
            state["people"] = await session.scalar(select(func.count()).select_from(Person))

    run(runtime_database_url, event_setup)
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["ФИО", "Логин в Тайме", "Telegram-тег"])
    sheet.append(
        ["Синтетический Участник Импорта", "synthetic.import.workflow", "@synthetic_import"]
    )
    content = io.BytesIO()
    workbook.save(content)
    headers = {"Authorization": "Bearer " + data["key"]}
    with TestClient(http_app(runtime_database_url)) as client:
        preview = client.post(
            "/api/v1/events/" + state["event"] + "/attendance-imports",
            headers={**headers, "Idempotency-Key": "preview-import"},
            files={
                "file": (
                    "attendance.xlsx",
                    content.getvalue(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        assert preview.status_code == 201, preview.text
        batch = preview.json()
        assert batch["new"] == 1 and batch["status"] == "preview"

        async def preview_check(sessions, _):
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(Attendance)) == 0
                assert (
                    await session.scalar(select(func.count()).select_from(Person))
                    == state["people"]
                )
                assert await session.scalar(select(func.count()).select_from(AttendanceImport)) == 1

        run(runtime_database_url, preview_check)
        path, body = (
            "/api/v1/attendance-imports/" + batch["id"] + "/confirm",
            {"version": batch["version"]},
        )
        confirmed = client.post(
            path, headers={**headers, "Idempotency-Key": "confirm-import"}, json=body
        )
        assert confirmed.status_code == 200 and confirmed.json()["new"] == 1, confirmed.text
        replay = client.post(
            path, headers={**headers, "Idempotency-Key": "confirm-import"}, json=body
        )
        assert replay.status_code == 200 and replay.json() == confirmed.json()
        another = client.post(
            path, headers={**headers, "Idempotency-Key": "confirm-import-again"}, json=body
        )
        assert another.status_code == 200 and another.json() == confirmed.json()
        roster = client.get("/api/v1/events/" + state["event"] + "/participants", headers=headers)
        assert roster.status_code == 200 and len(roster.json()["items"]) == 1

        async def final_check(sessions, _):
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(Attendance)) == 1
                assert (
                    await session.scalar(select(func.count()).select_from(Person))
                    == state["people"] + 1
                )

        run(runtime_database_url, final_check)


def test_openapi_paths_security_requests_and_response_headers(runtime_database_url):
    data = prepare(runtime_database_url)
    with TestClient(http_app(runtime_database_url)) as client:
        schema = client.get("/openapi.json").json()
        paths = schema["paths"]
        for created_path in (
            "/api/v1/events",
            "/api/v1/links",
            "/api/v1/events/{public_id}/attendance-imports",
            "/api/v1/webhook-endpoints",
        ):
            assert "201" in paths[created_path]["post"]["responses"]
        required = {
            "/api/v1/events",
            "/api/v1/events/{public_id}",
            "/api/v1/events/{public_id}/publish",
            "/api/v1/events/{public_id}/invite",
            "/api/v1/events/{public_id}/registrations/me",
            "/api/v1/events/{public_id}/participants",
            "/api/v1/events/{public_id}/attendance-imports",
            "/api/v1/attendance-imports/{batch_id}/confirm",
            "/api/v1/links",
            "/api/v1/links/{link_id}/exchange",
            "/api/v1/changes",
            "/api/v1/webhook-endpoints",
        }
        assert required <= paths.keys()
        assert any(
            value.get("type") == "http" and value.get("scheme") == "bearer"
            for value in schema["components"]["securitySchemes"].values()
        )
        for path in required:
            for method, operation in paths[path].items():
                if method in {"get", "post", "patch", "put", "delete"}:
                    assert operation.get("security"), (path, method)
        link_schema = paths["/api/v1/links"]["post"]["requestBody"]["content"]["application/json"][
            "schema"
        ]
        if "$ref" in link_schema:
            link_schema = schema["components"]["schemas"][link_schema["$ref"].split("/")[-1]]
        assert link_schema["properties"]["telegram_id"]["type"] == "integer"
        assert link_schema.get("additionalProperties") is False
        multipart = paths["/api/v1/events/{public_id}/attendance-imports"]["post"]["requestBody"][
            "content"
        ]
        assert "multipart/form-data" in multipart
        result = client.get("/api/v1/changes", headers={"Authorization": "Bearer " + data["key"]})
        assert result.status_code == 200
        assert result.headers["Cache-Control"] == "no-store"
        assert result.headers["X-Request-ID"]


def example_module():
    path = Path(__file__).resolve().parents[1] / "examples" / "club_api_client.py"
    spec = importlib.util.spec_from_file_location("offline_club_api_example", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_example_client_headers_and_raw_webhook_verification():
    example = example_module()
    observed = []

    def transport(request):
        observed.append(request)
        return httpx.Response(200, json={"id": "event", "version": 1, "status": "draft"})

    with example.ClubApiClient(
        "https://api.example.org",
        "cck_OFFLINE",
        "ccu_OFFLINE",
        transport=httpx.MockTransport(transport),
    ) as client:
        client.create({"title": "Client example"}, idempotency_key="stable-create")
        client.publish("event", 1, idempotency_key="stable-publish")
        client.register("event", idempotency_key="stable-registration")
        client.changes(15)
    assert observed[0].url.path == "/api/v1/events"
    assert observed[0].headers["Authorization"] == "Bearer cck_OFFLINE"
    assert observed[0].headers["Idempotency-Key"] == "stable-create"
    assert observed[1].headers["If-Match"] == '"1"'
    assert observed[2].headers["X-User-Token"] == "ccu_OFFLINE"
    assert "X-User-Token" not in observed[3].headers
    assert observed[3].url.params["cursor"] == "15"
    raw, timestamp, secret = (
        b'{"delivery_id":"offline","value":1}',
        1800000000,
        "offline-signing-secret",
    )
    headers = signed_headers(secret, "offline", raw, timestamp)
    assert example.verify_webhook_signature(secret, raw, headers, now=timestamp)
    assert example.verify_webhook_signature(secret, raw, headers, now=timestamp + 300)
    assert not example.verify_webhook_signature(secret, raw, headers, now=timestamp + 301)
    assert not example.verify_webhook_signature(secret, raw, headers, now=timestamp - 301)
    assert not example.verify_webhook_signature(secret, raw + b" ", headers, now=timestamp)
    assert not example.verify_webhook_signature("other-secret", raw, headers, now=timestamp)
    assert not example.verify_webhook_signature(
        secret, raw, {**headers, "X-CU-Clubs-Timestamp": "bad"}, now=timestamp
    )


def test_delivery_status_retry_is_client_bound_and_idempotent(runtime_database_url, monkeypatch):
    from cu_clubs import api_service

    async def public_url(_):
        return "public.example", "93.184.216.34"

    monkeypatch.setattr(api_service, "public_target", public_url)
    data = prepare(runtime_database_url)
    headers = {"Authorization": "Bearer " + data["key"]}
    with TestClient(http_app(runtime_database_url)) as client:
        endpoint = client.post(
            "/api/v1/webhook-endpoints",
            json={"url": "https://public.example/hook"},
            headers={**headers, "Idempotency-Key": "endpoint-delta"},
        )
        assert endpoint.status_code == 201
        endpoint_id = endpoint.json()["id"]
        exposed = client.get("/api/v1/webhook-endpoints", headers=headers).json()["items"][0]
        assert "signing_secret" not in exposed and "secret_encrypted" not in exposed
        created = client.post(
            "/api/v1/events",
            json=payload(),
            headers={**headers, "Idempotency-Key": "delivery-event"},
        ).json()
        result = client.post(
            f"/api/v1/events/{created['id']}/publish",
            headers={
                **headers,
                "Idempotency-Key": "delivery-publish",
                "If-Match": str(created["version"]),
            },
        )
        assert result.status_code == 200
        delivery_id = client.get("/api/v1/webhook-deliveries", headers=headers).json()["items"][0][
            "id"
        ]
        other = {}

        async def setup(sessions, _):
            async with sessions.begin() as session:
                row = await session.get(ApiDelivery, UUID(delivery_id))
                row.status, row.attempts = "failed", 8
                issued = await ApiService(session, api_settings()).issue_client(
                    LEADER, "cinema", "Other key", list(SCOPES)
                )
                other["key"] = issued["api_key"]

        run(runtime_database_url, setup)
        route = f"/api/v1/webhook-deliveries/{delivery_id}/retry"
        denied = client.post(
            route,
            headers={"Authorization": "Bearer " + other["key"], "Idempotency-Key": "other-retry"},
        )
        assert denied.status_code == 404
        retry_headers = {**headers, "Idempotency-Key": "own-retry"}
        first, second = (
            client.post(route, headers=retry_headers),
            client.post(route, headers=retry_headers),
        )
        assert first.status_code == 200 and second.status_code == 200
        assert first.json() == second.json() == {"id": delivery_id, "status": "pending"}
        assert second.headers["Idempotency-Replayed"] == "true"
        client.delete(
            f"/api/v1/webhook-endpoints/{endpoint_id}",
            headers={**headers, "Idempotency-Key": "endpoint-stop"},
        )
        assert (
            client.post(route, headers={**headers, "Idempotency-Key": "revoked-retry"}).status_code
            == 404
        )
