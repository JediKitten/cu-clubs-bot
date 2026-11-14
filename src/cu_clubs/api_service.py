"""Integration authorization is bound to a club, never to a supplied actor ID."""

import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from cu_clubs.access import can_register
from cu_clubs.api_models import (
    ApiChange,
    ApiClient,
    ApiDelivery,
    ApiEndpoint,
    ApiGrant,
    ApiIdempotency,
    ApiLink,
    ApiRateLimit,
)
from cu_clubs.api_security import ApiError, decrypt, digest, encrypt, public_target
from cu_clubs.event_models import EventClub
from cu_clubs.events import EventService
from cu_clubs.models import Audit, Club, TelegramAccount
from cu_clubs.services import ClubService, PermissionDenied, ValidationError

SCOPES = (
    "events:read",
    "events:write",
    "registration:self",
    "attendance:write",
    "roster:read",
    "changes:read",
)
CHANGE_TYPES = (
    "event.published",
    "event.updated",
    "event.cancelled",
    "registration.changed",
    "attendance.changed",
)


def now():
    return datetime.now(UTC)


def as_uuid(raw):
    try:
        return UUID(str(raw))
    except (ValueError, TypeError, AttributeError):
        raise ApiError(404, "NOT_FOUND", "Resource not found") from None


def client_dto(client, club):
    return {
        "id": client.id.hex,
        "name": client.name,
        "club_id": club.public_id,
        "club_name": club.name,
        "scopes": client.scopes,
        "key_prefix": client.key_prefix,
        "active": client.revoked_at is None,
        "created_at": client.created_at.isoformat(),
    }


def change_dto(change):
    return {
        "id": change.id,
        "type": change.kind,
        "event_id": change.event_public_id,
        "version": change.version,
        "occurred_at": change.occurred_at.isoformat(),
    }


async def record_change(session, event, kind):
    if kind not in CHANGE_TYPES:
        return
    # Sequence allocation alone does not order commits. Hold this lock until
    # commit so polling a higher cursor can never skip an uncommitted lower one.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended('cu-clubs:changes-order',0))")
    )
    clubs = [
        str(value)
        for value in await session.scalars(
            select(EventClub.club_id).where(EventClub.event_id == event.id)
        )
    ]
    row = ApiChange(
        event_id=event.id,
        event_public_id=event.public_id,
        kind=kind,
        version=event.version,
        clubs=clubs,
    )
    session.add(row)
    await session.flush()
    endpoints = await session.scalars(
        select(ApiEndpoint)
        .join(ApiClient)
        .where(
            ApiEndpoint.revoked_at.is_(None),
            ApiClient.revoked_at.is_(None),
            ApiClient.club_id.in_([UUID(c) for c in clubs]),
        )
    )
    for endpoint in endpoints:
        if kind in endpoint.event_types:
            session.add(ApiDelivery(endpoint_id=endpoint.id, change_id=row.id))


class ApiService:
    def __init__(self, session, settings):
        self.session, self.settings = session, settings
        self.clubs = ClubService(session)

    async def _leader(self, actor, club):
        rights = await self.clubs.permissions(actor, club.public_id)
        if not (rights["is_admin"] or rights["is_leader"]):
            raise PermissionDenied("Интеграциями управляет лидер клуба или администратор.")

    async def issue_client(self, actor, club_id, name, scopes):
        club = await self.clubs._club(club_id)
        await self._leader(actor, club)
        if not isinstance(name, str) or not 2 <= len(name.strip()) <= 120:
            raise ValidationError("Название интеграции: от 2 до 120 символов.")
        if not scopes or not set(scopes) <= set(SCOPES):
            raise ValidationError("Неизвестные права интеграции.")
        active = await self.session.scalar(
            select(func.count())
            .select_from(ApiClient)
            .where(ApiClient.club_id == club.id, ApiClient.revoked_at.is_(None))
        )
        if active >= 20:
            raise ValidationError(
                "Сначала отзови неиспользуемые ключи: максимум 20 активных интеграций."
            )
        token = "cck_" + secrets.token_urlsafe(32)
        row = ApiClient(
            club_id=club.id,
            name=name.strip(),
            key_hash=digest(token),
            key_prefix=token[:10],
            scopes=sorted(set(scopes)),
            issuer_telegram_id=actor,
        )
        self.session.add(row)
        await self.session.flush()
        self.session.add(
            Audit(
                actor_telegram_id=actor,
                action="api.client.issued",
                entity_id=row.id,
                details={"club": club.public_id, "scopes": row.scopes},
            )
        )
        return {**client_dto(row, club), "api_key": token}

    async def clients(self, actor, club_id):
        club = await self.clubs._club(club_id)
        await self._leader(actor, club)
        rows = await self.session.scalars(
            select(ApiClient)
            .where(ApiClient.club_id == club.id)
            .order_by(ApiClient.created_at.desc())
            .limit(100)
        )
        return [client_dto(row, club) for row in rows]

    async def revoke_client(self, actor, client_id):
        row = await self.session.get(ApiClient, as_uuid(client_id), with_for_update=True)
        if not row:
            raise ApiError(404, "NOT_FOUND", "Integration not found")
        club = await self.session.get(Club, row.club_id)
        await self._leader(actor, club)
        if row.revoked_at is None:
            row.revoked_at = now()
            self.session.add(
                Audit(
                    actor_telegram_id=actor,
                    action="api.client.revoked",
                    entity_id=row.id,
                    details={},
                )
            )
        return client_dto(row, club)

    async def authorize_client(self, client):
        if not client or client.revoked_at is not None:
            raise ApiError(401, "INVALID_API_KEY", "Invalid or revoked API key")
        club = await self.session.get(Club, client.club_id)
        try:
            await self._leader(client.issuer_telegram_id, club)
        except PermissionDenied:
            raise ApiError(
                403, "CLIENT_PERMISSION_REVOKED", "Issuer no longer manages this club"
            ) from None
        return client

    async def authenticate(self, raw, scope=None):
        if not raw or not raw.startswith("cck_") or len(raw) > 100:
            raise ApiError(401, "INVALID_API_KEY", "A club API key is required")
        row = await self.session.scalar(select(ApiClient).where(ApiClient.key_hash == digest(raw)))
        await self.authorize_client(row)
        self.scope(row, scope)
        return row

    @staticmethod
    def scope(client, scope):
        if scope and scope not in client.scopes:
            raise ApiError(403, "INSUFFICIENT_SCOPE", f"Required scope: {scope}")

    async def rate(self, client):
        bucket = int(now().timestamp()) // 60
        stmt = insert(ApiRateLimit).values(client_id=client.id, bucket=bucket, requests=1)
        count = await self.session.scalar(
            stmt.on_conflict_do_update(
                index_elements=[ApiRateLimit.client_id, ApiRateLimit.bucket],
                set_={"requests": ApiRateLimit.requests + 1},
            ).returning(ApiRateLimit.requests)
        )
        return count <= self.settings.api_rate_limit_per_minute

    async def event_access(self, client, public_id, *, own=True, lock=False):
        event = await EventService(self.session)._event(public_id, lock=lock)
        bound = await self.session.scalar(
            select(EventClub.event_id).where(
                EventClub.event_id == event.id, EventClub.club_id == client.club_id
            )
        )
        if own and not bound:
            raise ApiError(404, "NOT_FOUND", "Event not found")
        if not bound and (event.visibility != "open" or event.status not in {"published", "held"}):
            raise ApiError(404, "NOT_FOUND", "Event not found")
        return event, bool(bound)

    async def delegated_actor(self, client, raw):
        if not raw or not raw.startswith("ccu_") or len(raw) > 100:
            raise ApiError(401, "USER_LINK_REQUIRED", "A confirmed user token is required")
        grant = await self.session.scalar(
            select(ApiGrant).where(
                ApiGrant.client_id == client.id,
                ApiGrant.token_hash == digest(raw),
                ApiGrant.revoked_at.is_(None),
                ApiGrant.expires_at > now(),
            )
        )
        if not grant:
            raise ApiError(401, "INVALID_USER_TOKEN", "Invalid, expired or revoked user token")
        account = await self.session.scalar(
            select(TelegramAccount).where(TelegramAccount.person_id == grant.person_id)
        )
        if not account or not can_register(self.session, account.person):
            raise ApiError(403, "VERIFICATION_REQUIRED", "Student email verification is required")
        return account.telegram_id, grant

    async def create_link(self, client, telegram_id):
        self.scope(client, "registration:self")
        if (
            isinstance(telegram_id, bool)
            or not isinstance(telegram_id, int)
            or not 0 < telegram_id <= 9223372036854775807
        ):
            raise ApiError(
                422, "INVALID_TELEGRAM_ID", "A numeric Telegram ID is required", field="telegram_id"
            )
        approve, exchange = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        row = ApiLink(
            client_id=client.id,
            telegram_id=telegram_id,
            approve_hash=digest(approve),
            exchange_hash=digest(exchange),
            expires_at=now() + timedelta(minutes=10),
        )
        self.session.add(row)
        await self.session.flush()
        return {
            "id": row.id.hex,
            "verification_uri": f"https://t.me/{self.settings.telegram_bot_username}?start=l_{approve}",
            "exchange_code": exchange,
            "expires_at": row.expires_at.isoformat(),
            "status": "pending",
        }

    async def _link_actor(self, actor, row):
        if (
            row is None
            or row.expires_at <= now()
            or row.exchanged_at is not None
            or row.telegram_id != actor
        ):
            raise ValidationError(
                "Привязка недоступна, истекла или предназначена другому человеку."
            )
        client = await self.session.get(ApiClient, row.client_id)
        await self.authorize_client(client)
        self.scope(client, "registration:self")
        account = await self.clubs._account(actor)
        if not account:
            raise PermissionDenied("Сначала открой бота.")
        return account, client

    async def open_link(self, actor, raw):
        row = await self.session.scalar(
            select(ApiLink).where(ApiLink.approve_hash == digest(raw)).with_for_update()
        )
        return await self.link_view(actor, row)

    async def link_view(self, actor, row_or_id):
        row = (
            row_or_id
            if isinstance(row_or_id, ApiLink)
            else await self.session.get(ApiLink, as_uuid(row_or_id), with_for_update=True)
        )
        account, client = await self._link_actor(actor, row)
        club = await self.session.get(Club, client.club_id)
        return {
            "id": row.id.hex,
            "name": client.name,
            "club": club.name,
            "verified": account.person.kind == "student" and account.person.verified_at is not None,
            "eligible": can_register(self.session, account.person),
            "declared_category": account.person.declared_category,
            "approved": row.approved_at is not None,
        }

    async def approve_link(self, actor, link_id):
        row = await self.session.get(ApiLink, as_uuid(link_id), with_for_update=True)
        account, _ = await self._link_actor(actor, row)
        if not can_register(self.session, account.person):
            raise PermissionDenied("Подтверди студенческую почту перед привязкой.")
        if row.approved_at is None:
            row.approved_at, row.person_id = now(), account.person_id
            self.session.add(
                Audit(
                    actor_telegram_id=actor,
                    action="api.link.approved",
                    entity_id=row.id,
                    details={"client": str(row.client_id)},
                )
            )

    async def exchange(self, client, link_id, code):
        row = await self.session.get(ApiLink, as_uuid(link_id), with_for_update=True)
        if (
            row is None
            or row.client_id != client.id
            or not hmac.compare_digest(row.exchange_hash, digest(code))
        ):
            raise ApiError(404, "LINK_NOT_FOUND", "Link not found")
        if row.expires_at <= now() or row.exchanged_at:
            raise ApiError(409, "LINK_EXPIRED_OR_USED", "Link has expired or was exchanged")
        if row.approved_at is None:
            raise ApiError(409, "LINK_PENDING", "User confirmation is required")
        account = await self.clubs._account(row.telegram_id)
        if (
            not account
            or account.person_id != row.person_id
            or not can_register(self.session, account.person)
        ):
            raise ApiError(403, "VERIFICATION_REQUIRED", "Student verification is required")
        await self.session.execute(
            update(ApiGrant)
            .where(
                ApiGrant.client_id == client.id,
                ApiGrant.person_id == row.person_id,
                ApiGrant.revoked_at.is_(None),
            )
            .values(revoked_at=now())
        )
        token = "ccu_" + secrets.token_urlsafe(32)
        expires = now() + timedelta(days=self.settings.api_grant_ttl_days)
        grant = ApiGrant(
            client_id=client.id,
            person_id=row.person_id,
            token_hash=digest(token),
            expires_at=expires,
        )
        self.session.add(grant)
        row.exchanged_at = now()
        return {"user_token": token, "token_type": "Bearer", "expires_at": expires.isoformat()}

    async def grants(self, actor):
        account = await self.clubs._account(actor)
        if not account:
            return []
        rows = await self.session.execute(
            select(ApiGrant, ApiClient, Club)
            .join(ApiClient, ApiClient.id == ApiGrant.client_id)
            .join(Club, Club.id == ApiClient.club_id)
            .where(
                ApiGrant.person_id == account.person_id,
                ApiGrant.revoked_at.is_(None),
                ApiGrant.expires_at > now(),
                ApiClient.revoked_at.is_(None),
            )
        )
        return [
            {"id": grant.id.hex, "name": client.name, "club": club.name}
            for grant, client, club in rows
        ]

    async def revoke_grant(self, actor, grant_id):
        account = await self.clubs._account(actor)
        grant = await self.session.get(ApiGrant, as_uuid(grant_id), with_for_update=True)
        if not account or not grant or grant.person_id != account.person_id:
            raise PermissionDenied("Привязка недоступна.")
        grant.revoked_at = now()

    async def idempotent(self, client, namespace, key, fingerprint, operation, *, status=200):
        if (
            not key
            or not 1 <= len(key) <= 128
            or not key.isascii()
            or any(ord(c) < 33 for c in key)
        ):
            raise ApiError(
                422,
                "IDEMPOTENCY_KEY_REQUIRED",
                "Supply an Idempotency-Key of 1-128 printable ASCII characters",
            )
        key_hash = digest(key)
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"cu-clubs:api:{client.id}:{namespace}:{key_hash}"},
        )
        request_hash = digest(json.dumps(fingerprint, sort_keys=True, separators=(",", ":")))
        row = await self.session.get(ApiIdempotency, (client.id, namespace, key_hash))
        if row:
            if row.request_hash != request_hash:
                raise ApiError(
                    409, "IDEMPOTENCY_CONFLICT", "Idempotency-Key was used for another request"
                )
            return (
                decrypt(row.response_encrypted, self.settings.verification_secret),
                row.status_code,
                True,
            )
        response = await operation()
        self.session.add(
            ApiIdempotency(
                client_id=client.id,
                namespace=namespace,
                key_hash=key_hash,
                request_hash=request_hash,
                response_encrypted=encrypt(response, self.settings.verification_secret),
                status_code=status,
            )
        )
        return response, status, False

    async def changes(self, client, cursor=0, limit=100):
        self.scope(client, "changes:read")
        rows = list(
            await self.session.scalars(
                select(ApiChange)
                .where(ApiChange.id > cursor, ApiChange.clubs.contains([str(client.club_id)]))
                .order_by(ApiChange.id)
                .limit(limit + 1)
            )
        )
        page = rows[:limit]
        return {
            "items": [change_dto(row) for row in page],
            "next_cursor": page[-1].id if page else cursor,
            "has_more": len(rows) > limit,
        }

    async def endpoint(self, client, url, types):
        self.scope(client, "changes:read")
        if not types or not set(types) <= set(CHANGE_TYPES):
            raise ApiError(422, "INVALID_EVENT_TYPES", "Unsupported webhook event types")
        await public_target(url)
        existing = await self.session.scalar(
            select(ApiEndpoint.id).where(
                ApiEndpoint.client_id == client.id,
                ApiEndpoint.url == url,
                ApiEndpoint.revoked_at.is_(None),
            )
        )
        if existing:
            raise ApiError(409, "ENDPOINT_EXISTS", "An active endpoint already exists")
        secret = "ccw_" + secrets.token_urlsafe(32)
        row = ApiEndpoint(
            client_id=client.id,
            url=url,
            event_types=sorted(set(types)),
            secret_encrypted=encrypt(secret, self.settings.verification_secret),
        )
        self.session.add(row)
        await self.session.flush()
        return {
            "id": row.id.hex,
            "url": url,
            "event_types": row.event_types,
            "signing_secret": secret,
        }
