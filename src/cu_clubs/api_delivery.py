"""Leased, signed integration deliveries; public DNS is pinned through TLS."""

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import timedelta

import httpx
from sqlalchemy import and_, or_, select, update

from cu_clubs.api_models import ApiChange, ApiClient, ApiDelivery, ApiEndpoint
from cu_clubs.api_security import ApiError, decrypt, public_target
from cu_clubs.api_service import ApiService, change_dto, now


async def claim(sessions):
    async with sessions.begin() as session:
        row = await session.scalar(
            select(ApiDelivery)
            .where(
                or_(
                    and_(ApiDelivery.status == "pending", ApiDelivery.available_at <= now()),
                    and_(ApiDelivery.status == "processing", ApiDelivery.lease_until <= now()),
                )
            )
            .order_by(ApiDelivery.available_at, ApiDelivery.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        row.status, row.attempts, row.lease_until = (
            "processing",
            row.attempts + 1,
            now() + timedelta(seconds=90),
        )
        return {
            "id": row.id,
            "attempts": row.attempts,
            "endpoint_id": row.endpoint_id,
            "change_id": row.change_id,
        }


def signed_headers(secret, delivery_id, body, timestamp):
    signature = hmac.new(
        secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-CU-Clubs-Delivery": str(delivery_id),
        "X-CU-Clubs-Timestamp": str(timestamp),
        "X-CU-Clubs-Signature": "v1=" + signature,
    }


async def post_public(url, body, headers):
    hostname, ip = await public_target(url)
    original = httpx.URL(url)
    # The connection uses the already-validated IP, so a second DNS lookup cannot
    # rebind to localhost. TLS verifies the original hostname, not the pinned IP.
    target = original.copy_with(host=ip)
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
        async with client.stream(
            "POST",
            target,
            content=body,
            headers={**headers, "Host": original.netloc.decode()},
            extensions={"sni_hostname": hostname},
        ) as response:
            # No endpoint response bodies or credential-bearing URLs enter logs.
            return response.status_code, response.headers.get("Retry-After")


async def process(sessions, settings, item, *, sender=post_public):
    values = {"lease_until": None}
    try:
        async with sessions() as session:
            endpoint = await session.get(ApiEndpoint, item["endpoint_id"])
            client = await session.get(ApiClient, endpoint.client_id) if endpoint else None
            if not endpoint or endpoint.revoked_at is not None:
                raise ApiError(403, "REVOKED", "Endpoint revoked")
            service = ApiService(session, settings)
            await service.authorize_client(client)
            service.scope(client, "changes:read")
            change = await session.get(ApiChange, item["change_id"])
            if str(client.club_id) not in change.clubs or change.kind not in endpoint.event_types:
                raise ApiError(403, "REVOKED", "Delivery no longer authorized")
            await service.event_access(client, change.event_public_id)
            payload = {"delivery_id": str(item["id"]), **change_dto(change)}
            body = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
            url = endpoint.url
            secret = decrypt(endpoint.secret_encrypted, settings.verification_secret)
        headers = signed_headers(secret, item["id"], body, int(now().timestamp()))
        status, retry_after = await sender(url, body, headers)
        if 200 <= status < 300:
            values.update(status="sent", delivered_at=now(), last_error=None)
        elif status == 429 or status >= 500:
            try:
                delay = (
                    min(3600, max(1, int(retry_after)))
                    if retry_after
                    else min(3600, 2 ** item["attempts"])
                )
            except ValueError:
                delay = min(3600, 2 ** item["attempts"])
            values.update(
                status="pending" if item["attempts"] < 8 else "failed",
                available_at=now() + timedelta(seconds=delay),
                last_error=f"HTTP_{status}",
            )
        else:
            values.update(status="failed", last_error=f"HTTP_{status}")
    except ApiError as error:
        if error.status >= 500:
            values.update(
                status="pending" if item["attempts"] < 8 else "failed",
                available_at=now() + timedelta(seconds=min(3600, 2 ** item["attempts"])),
                last_error=error.code,
            )
        else:
            values.update(status="cancelled", last_error="AuthorizationOrTargetRejected")
    except Exception as error:
        # A timeout may follow a successful remote write: retry the SAME
        # delivery_id and require receiver deduplication instead of claiming once.
        values.update(
            status="pending" if item["attempts"] < 8 else "failed",
            available_at=now() + timedelta(seconds=min(3600, 2 ** item["attempts"])),
            last_error=type(error).__name__[:80],
        )
    async with sessions.begin() as session:
        await session.execute(
            update(ApiDelivery)
            .where(
                ApiDelivery.id == item["id"],
                ApiDelivery.status == "processing",
                ApiDelivery.attempts == item["attempts"],
            )
            .values(**values)
        )


async def loop(sessions, settings):
    while True:
        try:
            item = await claim(sessions)
            if item:
                await process(sessions, settings, item)
            else:
                await asyncio.sleep(1)
        except Exception as error:
            logging.getLogger(__name__).warning(
                "Integration delivery loop failed: %s", type(error).__name__
            )
            await asyncio.sleep(3)
