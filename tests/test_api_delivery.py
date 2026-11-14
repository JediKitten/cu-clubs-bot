"""Independent offline webhook/feed security checks; no network or real DNS."""

import asyncio
import hashlib
import hmac
import json
import socket
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select
from test_api_security import api_settings
from test_events import LEADER, published, seed
from test_events import sessions as sessions

from cu_clubs import api_delivery, api_security
from cu_clubs.api_models import ApiChange, ApiClient, ApiDelivery, ApiEndpoint
from cu_clubs.api_security import ApiError, encrypt, public_target
from cu_clubs.api_service import ApiService, record_change
from cu_clubs.event_models import Event

SIGNING_SECRET = "ccw_OFFLINE_SYNTHETIC_SIGNING_SECRET"


@pytest.mark.parametrize(
    "url,addresses",
    [
        ("http://public.example/hook", ["93.184.216.34"]),
        ("https://user:password@public.example/hook", ["93.184.216.34"]),
        ("https://public.example:8443/hook", ["93.184.216.34"]),
        ("https://public.example/hook#fragment", ["93.184.216.34"]),
        ("https://public.example/hook", ["127.0.0.1"]),
        ("https://public.example/hook", ["169.254.169.254"]),
        ("https://public.example/hook", ["10.0.0.1"]),
        ("https://public.example/hook", ["::1"]),
        ("https://public.example/hook", ["::ffff:127.0.0.1"]),
        ("https://public.example/hook", ["93.184.216.34", "192.168.1.1"]),
    ],
)
def test_ssrf_rejects_private_or_mixed_dns(monkeypatch, url, addresses):
    def fake_dns(host, port, **kwargs):
        return [
            (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
            for ip in addresses
        ]

    monkeypatch.setattr(api_security.socket, "getaddrinfo", fake_dns)
    with pytest.raises(ApiError) as error:
        asyncio.run(public_target(url))
    assert error.value.code == "INVALID_WEBHOOK_URL"


def test_webhook_pins_public_ip_and_preserves_tls_hostname(monkeypatch):
    observed = {}

    async def fake_target(url):
        observed["resolved"] = url
        return "public.example", "93.184.216.34"

    class OfflineClient:
        def __init__(self, **kwargs):
            observed["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        @asynccontextmanager
        async def stream(self, method, url, **kwargs):
            observed.update(method=method, url=url, request=kwargs)
            yield SimpleNamespace(status_code=307, headers={"Retry-After": "12"})

    monkeypatch.setattr(api_delivery, "public_target", fake_target)
    monkeypatch.setattr(api_delivery.httpx, "AsyncClient", OfflineClient)
    result = asyncio.run(
        api_delivery.post_public(
            "https://public.example/hook?q=kept", b"payload", {"X-Test": "value"}
        )
    )
    assert result == (307, "12")
    assert observed["url"].host == "93.184.216.34"
    assert observed["url"].query == b"q=kept"
    assert observed["request"]["headers"]["Host"] == "public.example"
    assert observed["request"]["extensions"]["sni_hostname"] == "public.example"
    assert observed["client"]["trust_env"] is False
    assert observed["client"]["follow_redirects"] is False


def test_signature_is_hmac_of_timestamp_and_original_bytes():
    body, timestamp = b'{"text":"different spacing matters"}', 1800000000
    result = api_delivery.signed_headers(SIGNING_SECRET, "stable-id", body, timestamp)
    expected = hmac.new(
        SIGNING_SECRET.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    assert result["X-CU-Clubs-Signature"] == "v1=" + expected
    assert result["X-CU-Clubs-Delivery"] == "stable-id"
    assert (
        api_delivery.signed_headers(SIGNING_SECRET, "stable-id", body + b" ", timestamp)[
            "X-CU-Clubs-Signature"
        ]
        != result["X-CU-Clubs-Signature"]
    )


async def queued_delivery(sessions):
    await seed(sessions)
    event = await published(sessions)
    async with sessions.begin() as session:
        api = ApiService(session, api_settings())
        issued = await api.issue_client(
            LEADER, "cinema", "Offline webhook", ["events:read", "changes:read"]
        )
        client = await api.authenticate(issued["api_key"])
        endpoint = ApiEndpoint(
            client_id=client.id,
            url="https://public.example/hook",
            secret_encrypted=encrypt(SIGNING_SECRET, api_settings().verification_secret),
            event_types=["event.updated", "attendance.changed"],
        )
        session.add(endpoint)
        await session.flush()
        owner = await session.scalar(select(Event).where(Event.public_id == event["public_id"]))
        owner.post_registration_text = "DO_NOT_EXPOSE_POST_REGISTRATION_SECRET"
        await record_change(session, owner, "event.updated")
        await session.flush()
        delivery = await session.scalar(
            select(ApiDelivery).where(ApiDelivery.endpoint_id == endpoint.id)
        )
        return {
            "id": delivery.id,
            "endpoint_id": endpoint.id,
            "key": issued["api_key"],
            "client_id": client.id,
            "event": event["public_id"],
        }


def test_retry_delivery_id_dedup_and_no_secrets_in_feed(sessions):
    async def scenario():
        data = await queued_delivery(sessions)
        received, effects = [], set()

        async def sender(url, body, headers):
            payload = json.loads(body)
            signature = headers["X-CU-Clubs-Signature"]
            expected = api_delivery.signed_headers(
                SIGNING_SECRET, payload["delivery_id"], body, int(headers["X-CU-Clubs-Timestamp"])
            )["X-CU-Clubs-Signature"]
            assert hmac.compare_digest(signature, expected)
            effects.add(payload["delivery_id"])
            received.append(payload)
            if len(received) == 1:
                raise httpx.ReadTimeout("DO_NOT_EXPOSE_URL_OR_CREDENTIAL")
            return 204, None

        first = await api_delivery.claim(sessions)
        assert first["id"] == data["id"] and first["attempts"] == 1
        await api_delivery.process(sessions, api_settings(), first, sender=sender)
        async with sessions.begin() as session:
            delivery = await session.get(ApiDelivery, data["id"])
            assert delivery.status == "pending" and delivery.last_error == "ReadTimeout"
            assert "DO_NOT_EXPOSE" not in delivery.last_error
            delivery.available_at = datetime.now(UTC) - timedelta(seconds=1)
        second = await api_delivery.claim(sessions)
        assert second["id"] == first["id"] and second["attempts"] == 2
        await api_delivery.process(sessions, api_settings(), second, sender=sender)
        assert len(received) == 2 and len(effects) == 1
        assert received[0] == received[1]
        assert set(received[0]) == {
            "delivery_id",
            "id",
            "type",
            "event_id",
            "version",
            "occurred_at",
        }
        async with sessions() as session:
            row = await session.get(ApiDelivery, data["id"])
            assert row.status == "sent" and row.delivered_at is not None
            api = ApiService(session, api_settings())
            client = await api.authenticate(data["key"])
            feed = await api.changes(client)
            assert data["key"] not in json.dumps(feed)
            assert SIGNING_SECRET not in json.dumps(feed)
            assert "POST_REGISTRATION_SECRET" not in json.dumps(feed)

    asyncio.run(scenario())


@pytest.mark.parametrize("revoke", ["client", "endpoint"])
def test_revoked_endpoint_and_client_never_reach_sender(sessions, revoke):
    async def scenario():
        data = await queued_delivery(sessions)
        item = await api_delivery.claim(sessions)
        async with sessions.begin() as session:
            model, key = (
                (ApiClient, data["client_id"])
                if revoke == "client"
                else (ApiEndpoint, data["endpoint_id"])
            )
            row = await session.get(model, key)
            row.revoked_at = datetime.now(UTC)

        async def sender(*args):
            pytest.fail("Revoked API key reached webhook sender")

        await api_delivery.process(sessions, api_settings(), item, sender=sender)
        async with sessions() as session:
            delivery = await session.get(ApiDelivery, data["id"])
            assert delivery.status == "cancelled"

    asyncio.run(scenario())


def test_change_cursor_follows_commit_order(sessions):
    async def scenario():
        await seed(sessions)
        event = await published(sessions)
        async with sessions.begin() as session:
            api = ApiService(session, api_settings())
            issued = await api.issue_client(LEADER, "cinema", "Cursor client", ["changes:read"])
            baseline = await session.scalar(select(func.max(ApiChange.id))) or 0
        first_allocated, release_first, second_started = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )

        async def first_transaction():
            async with sessions.begin() as session:
                owner = await session.scalar(
                    select(Event).where(Event.public_id == event["public_id"])
                )
                await record_change(session, owner, "event.updated")
                first_allocated.set()
                await release_first.wait()

        async def second_transaction():
            await first_allocated.wait()
            async with sessions.begin() as session:
                owner = await session.scalar(
                    select(Event).where(Event.public_id == event["public_id"])
                )
                second_started.set()
                await record_change(session, owner, "event.cancelled")

        first, second = (
            asyncio.create_task(first_transaction()),
            asyncio.create_task(second_transaction()),
        )
        try:
            await second_started.wait()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(second), timeout=0.1)
            async with sessions() as session:
                api = ApiService(session, api_settings())
                client = await api.authenticate(issued["api_key"])
                assert (await api.changes(client, baseline))["items"] == []
            release_first.set()
            await asyncio.gather(first, second)
            async with sessions() as session:
                api = ApiService(session, api_settings())
                client = await api.authenticate(issued["api_key"])
                feed = await api.changes(client, baseline)
                assert [row["type"] for row in feed["items"]] == [
                    "event.updated",
                    "event.cancelled",
                ]
                assert feed["next_cursor"] == feed["items"][-1]["id"]
        finally:
            release_first.set()
            await asyncio.gather(first, second, return_exceptions=True)

    asyncio.run(scenario())
