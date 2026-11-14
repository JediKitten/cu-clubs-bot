"""Versioned club API over the exact services used by Telegram."""

import asyncio
import hashlib
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, Header, Query, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from cu_clubs.api_models import ApiDelivery, ApiEndpoint
from cu_clubs.api_security import ApiError
from cu_clubs.api_service import CHANGE_TYPES, ApiService, as_uuid
from cu_clubs.attendance import AttendanceService
from cu_clubs.attendance_models import Attendance, AttendanceImport
from cu_clubs.attendance_xlsx import MAX_INPUT_BYTES, export_attendance_xlsx, parse_attendance_xlsx
from cu_clubs.event_models import Event
from cu_clubs.events import EventNotFound, EventService
from cu_clubs.services import ClubNotFound, PermissionDenied, ValidationError


class ErrorDetail(BaseModel):
    code: str
    message: str
    field: str | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorDetail
    request_id: str


router = APIRouter(
    prefix="/api/v1",
    tags=["club API"],
    responses={
        code: {"model": ErrorEnvelope, "description": description}
        for code, description in {
            401: "Invalid credentials",
            403: "Scope or permission denied",
            404: "Unavailable resource",
            409: "State or idempotency conflict",
            412: "Version conflict",
            413: "Body too large",
            422: "Invalid input",
            428: "If-Match required",
            429: "Rate limit",
            503: "Integration unavailable",
        }.items()
    },
)
bearer = HTTPBearer(
    auto_error=False,
    description=(
        "Club key issued by its leader in Telegram. "
        "User actions require X-User-Token from explicit delegation."
    ),
)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


EMPTY_BODY = Body(default_factory=Input)


class EventFields(Input):
    title: str | None = Field(default=None, max_length=45)
    description: str | None = Field(default=None, max_length=250)
    post_registration_text: str | None = Field(default=None, max_length=150)
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    registration_deadline: AwareDatetime | None = None
    capacity: int | None = Field(default=None, gt=0)
    expected_attendance: int | None = Field(default=None, gt=0)
    venue_type: Literal["campus", "external", "online"] = "campus"
    room: str | None = Field(default=None, max_length=300)
    venue_details: str = Field(default="", max_length=2000)
    needs_talk: bool = False
    needs_microphones: bool = False
    visibility: Literal["open", "closed"] = "open"


class LinkInput(Input):
    telegram_id: int = Field(gt=0, le=9223372036854775807, strict=True)


class ExchangeInput(Input):
    exchange_code: str = Field(min_length=16, max_length=100)


class InviteInput(Input):
    club_id: str = Field(min_length=1, max_length=32)


class AttendanceInput(Input):
    full_name: str = Field(min_length=2, max_length=300)
    time_login: str = Field(min_length=1, max_length=300)
    telegram_tag: str | None = Field(default=None, max_length=65)


class ConfirmImportInput(Input):
    version: int = Field(gt=0, strict=True)


class ResolveImportInput(ConfirmImportInput):
    rows: list[int] = Field(min_length=1, max_length=5000)


class EndpointInput(Input):
    url: str = Field(min_length=10, max_length=2000)
    event_types: list[str] = Field(
        default_factory=lambda: list(CHANGE_TYPES), min_length=1, max_length=5
    )


class EventOutput(BaseModel):
    id: str
    title: str
    description: str
    status: str
    visibility: str
    version: int
    clubs: list[dict]
    starts_at: AwareDatetime | None
    ends_at: AwareDatetime | None
    registration_deadline: AwareDatetime | None
    registered_count: int
    room: str | None = None
    post_registration_text: str | None = None
    registered: bool | None = None
    registered_at: AwareDatetime | None = None


class LinkOutput(BaseModel):
    id: str
    verification_uri: str
    exchange_code: str
    expires_at: AwareDatetime
    status: str


class TokenOutput(BaseModel):
    user_token: str
    token_type: str
    expires_at: AwareDatetime


@dataclass
class Context:
    key: str
    user_token: str | None


async def context(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
    user_token: Annotated[str | None, Header(alias="X-User-Token")] = None,
):
    if not request.app.state.settings.api_enabled:
        raise ApiError(503, "API_DISABLED", "API is disabled")
    key = credentials.credentials if credentials else ""
    async with request.app.state.sessions.begin() as session:
        service = ApiService(session, request.app.state.settings)
        client = await service.authenticate(key)
        permitted = await service.rate(client)
    if not permitted:
        raise ApiError(429, "RATE_LIMITED", "Request limit exceeded; retry next minute")
    return Context(key, user_token)


Ctx = Annotated[Context, Depends(context)]


@asynccontextmanager
async def authorized(request, ctx, scope, *, write=False):
    async with request.app.state.sessions.begin() as session:
        if write:
            await session.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended('cu-clubs:identity-attendance',0))"
                )
            )
        api = ApiService(session, request.app.state.settings)
        client = await api.authenticate(ctx.key, scope)
        yield api, client


def version_header(raw):
    if raw is None:
        raise ApiError(428, "IF_MATCH_REQUIRED", "Supply If-Match with the current event version")
    match = re.fullmatch(r'(?:"([1-9][0-9]*)"|([1-9][0-9]*))', raw)
    if not match:
        raise ApiError(422, "INVALID_IF_MATCH", "If-Match must contain a positive integer version")
    return int(match[1] or match[2])


def event_dto(dto, *, private=False, personal=False):
    fields = {
        "public_id",
        "title",
        "description",
        "status",
        "visibility",
        "version",
        "clubs",
        "starts_at",
        "ends_at",
        "registration_deadline",
        "venue_type",
        "room",
        "venue_details",
        "capacity",
        "expected_attendance",
        "needs_talk",
        "needs_microphones",
        "registered_count",
        "registered",
        "registered_at",
        "created_at",
        "updated_at",
    }
    fields -= {"registered", "registered_at"}
    if personal:
        fields |= {"registered", "registered_at"}
    if private or (personal and dto.get("registered")):
        fields.add("post_registration_text")
    result = {key: value for key, value in dto.items() if key in fields}
    result["id"] = dto["public_id"]
    return result


def json_result(data, *, status=200, replay=False):
    headers = {"Idempotency-Replayed": str(replay).lower()}
    if isinstance(data, dict) and isinstance(data.get("version"), int):
        headers["ETag"] = f'"{data["version"]}"'
    return JSONResponse(data, status_code=status, headers=headers)


async def mutate(request, ctx, scope, body, action, *, status=200, event_id=None, user=False):
    async with authorized(request, ctx, scope, write=True) as (api, client):
        actor, grant = (
            (await api.delegated_actor(client, ctx.user_token))
            if user
            else (client.issuer_telegram_id, None)
        )
        if event_id:
            await api.event_access(client, event_id, lock=True)
        fingerprint = {
            "body": body,
            "user": grant.id.hex if grant else None,
            "if_match": request.headers.get("If-Match"),
        }
        data, code, replay = await api.idempotent(
            client,
            request.method + ":" + request.url.path,
            request.headers.get("Idempotency-Key"),
            fingerprint,
            lambda: action(api, client, actor),
            status=status,
        )
        return json_result(data, status=code, replay=replay)


async def event_version(api, client, public_id, raw):
    event, _ = await api.event_access(client, public_id, lock=True)
    expected = version_header(raw)
    if event.version != expected:
        raise ApiError(412, "VERSION_CONFLICT", "Event changed; fetch it again")
    return event, expected


@router.get("/clubs/{club_id}", summary="Public club card (events:read)")
async def club(request: Request, club_id: str, ctx: Ctx):
    async with authorized(request, ctx, "events:read") as (api, client):
        result = await api.clubs.get_club(club_id)
        if result is None:
            raise ApiError(404, "NOT_FOUND", "Club not found")
        return result


@router.get("/events", summary="Open calendar, cursor pagination (events:read)")
async def events(
    request: Request,
    ctx: Ctx,
    starts: Annotated[AwareDatetime, Query(alias="from")],
    ends: Annotated[AwareDatetime, Query(alias="to")],
    club_id: str | None = None,
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
):
    if ends <= starts or (ends - starts).days > 366:
        raise ApiError(422, "INVALID_RANGE", "Use an increasing range no longer than 366 days")
    async with authorized(request, ctx, "events:read") as (api, client):
        # No issuer/admin identity is used for the public catalogue.
        rows = await EventService(api.session).public_events(starts, ends, club_id)
        page = rows[cursor : cursor + limit]
        return {
            "items": [event_dto(row) for row in page],
            "next_cursor": cursor + len(page) if cursor + len(page) < len(rows) else None,
        }


@router.get(
    "/events/{public_id}", summary="Read event; private events stay club-scoped (events:read)"
)
async def event(request: Request, public_id: str, ctx: Ctx):
    async with authorized(request, ctx, "events:read") as (api, client):
        row, bound = await api.event_access(client, public_id, own=False)
        actor = None
        if ctx.user_token:
            if not bound:
                raise ApiError(404, "NOT_FOUND", "Event not found")
            api.scope(client, "registration:self")
            actor, _ = await api.delegated_actor(client, ctx.user_token)
        dto = await EventService(api.session)._dto(row, actor)
        return json_result(
            event_dto(
                dto,
                private=bound and "events:write" in client.scopes,
                personal=ctx.user_token is not None,
            )
        )


@router.post("/events", summary="Create a draft for the key's club (events:write)")
async def create_event(request: Request, body: EventFields, ctx: Ctx):
    fields = body.model_dump(mode="json", exclude_unset=True)

    async def action(api, client, actor):
        own = await api.session.get(Club, client.club_id)
        dto = await EventService(api.session).create_draft(actor, own.public_id, fields)
        return event_dto(dto, private=True)

    from cu_clubs.models import Club

    return await mutate(request, ctx, "events:write", fields, action, status=201)


@router.patch("/events/{public_id}", summary="Change event with If-Match (events:write)")
async def edit_event(request: Request, public_id: str, body: EventFields, ctx: Ctx):
    fields = body.model_dump(mode="json", exclude_unset=True)

    async def action(api, client, actor):
        event, expected = await event_version(
            api, client, public_id, request.headers.get("If-Match")
        )
        service = EventService(api.session)
        method = service.update_event if event.status == "published" else service.update_draft
        return event_dto(await method(actor, public_id, fields, expected), private=True)

    return await mutate(request, ctx, "events:write", fields, action, event_id=public_id)


@router.post("/events/{public_id}/publish", summary="Publish current draft (events:write)")
async def publish_event(request: Request, public_id: str, ctx: Ctx, body: Input = EMPTY_BODY):
    async def action(api, client, actor):
        _, expected = await event_version(api, client, public_id, request.headers.get("If-Match"))
        return event_dto(
            await EventService(api.session).publish(actor, public_id, expected), private=True
        )

    return await mutate(request, ctx, "events:write", body.model_dump(), action, event_id=public_id)


@router.post(
    "/events/{public_id}/invite",
    summary="Invite partner; acceptance remains personal in Telegram (events:write)",
)
async def invite(request: Request, public_id: str, body: InviteInput, ctx: Ctx):
    async def action(api, client, actor):
        _, expected = await event_version(api, client, public_id, request.headers.get("If-Match"))
        return event_dto(
            await EventService(api.session).invite_partner(
                actor, public_id, body.club_id, expected
            ),
            private=True,
        )

    return await mutate(request, ctx, "events:write", body.model_dump(), action, event_id=public_id)


@router.post("/events/{public_id}/cancel", summary="Cancel published event (events:write)")
async def cancel_event(request: Request, public_id: str, ctx: Ctx, body: Input = EMPTY_BODY):
    async def action(api, client, actor):
        _, expected = await event_version(api, client, public_id, request.headers.get("If-Match"))
        return event_dto(
            await EventService(api.session).cancel_event(actor, public_id, expected), private=True
        )

    return await mutate(request, ctx, "events:write", body.model_dump(), action, event_id=public_id)


@router.post(
    "/links", summary="Start explicit, numeric-ID-bound user authorization (registration:self)"
)
async def create_link(request: Request, body: LinkInput, ctx: Ctx):
    return await mutate(
        request,
        ctx,
        "registration:self",
        body.model_dump(),
        lambda api, client, actor: api.create_link(client, body.telegram_id),
        status=201,
    )


@router.post(
    "/links/{link_id}/exchange",
    summary="Exchange confirmed single-use code for a user token (registration:self)",
)
async def exchange(request: Request, link_id: str, body: ExchangeInput, ctx: Ctx):
    return await mutate(
        request,
        ctx,
        "registration:self",
        body.model_dump(),
        lambda api, client, actor: api.exchange(client, link_id, body.exchange_code),
    )


@router.get("/me", summary="Own linked profile (registration:self + X-User-Token)")
async def me(request: Request, ctx: Ctx):
    async with authorized(request, ctx, "registration:self") as (api, client):
        actor, _ = await api.delegated_actor(client, ctx.user_token)
        account = await api.clubs._account(actor)
        return {
            "person_id": str(account.person_id),
            "full_name": account.person.full_name,
            "time_login": account.person.time_login,
            "verified": True,
        }


@router.put(
    "/events/{public_id}/registrations/me",
    summary="Register the explicitly linked student (registration:self)",
)
async def register(request: Request, public_id: str, ctx: Ctx, body: Input = EMPTY_BODY):
    async def action(api, client, actor):
        return event_dto(
            await EventService(api.session).register(actor, public_id, source="api", via_link=True),
            personal=True,
        )

    return await mutate(
        request, ctx, "registration:self", body.model_dump(), action, event_id=public_id, user=True
    )


@router.delete(
    "/events/{public_id}/registrations/me", summary="Cancel own registration (registration:self)"
)
async def unregister(request: Request, public_id: str, ctx: Ctx):
    async def action(api, client, actor):
        return event_dto(
            await EventService(api.session).cancel_registration(actor, public_id), personal=True
        )

    return await mutate(
        request, ctx, "registration:self", {}, action, event_id=public_id, user=True
    )


@router.get(
    "/events/{public_id}/participants/me",
    summary="Own registration and attendance (registration:self)",
)
async def participant(request: Request, public_id: str, ctx: Ctx):
    async with authorized(request, ctx, "registration:self") as (api, client):
        actor, _ = await api.delegated_actor(client, ctx.user_token)
        event, _ = await api.event_access(client, public_id)
        account = await api.clubs._account(actor)
        registration = await EventService(api.session)._registration(event, actor)
        attendance = await api.session.get(Attendance, (event.id, account.person_id))
        return {
            "event_id": public_id,
            "registration_status": registration.status if registration else None,
            "attended": attendance is not None,
        }


@router.get(
    "/events/{public_id}/participants", summary="Roster with pagination or XLSX (roster:read)"
)
async def participants(
    request: Request,
    public_id: str,
    ctx: Ctx,
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    format: Literal["json", "xlsx"] = "json",
    all_participants: bool = True,
):
    async with authorized(request, ctx, "roster:read") as (api, client):
        await api.event_access(client, public_id)
        rows = await AttendanceService(api.session, api.settings.verification_secret).roster(
            client.issuer_telegram_id, public_id, all_participants=all_participants
        )
        if format == "xlsx":
            return Response(
                export_attendance_xlsx(rows),
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": 'attachment; filename="attendance.xlsx"'},
            )
        return {
            "items": rows[cursor : cursor + limit],
            "next_cursor": cursor + limit if cursor + limit < len(rows) else None,
        }


@router.post(
    "/events/{public_id}/attendance",
    summary="Mark a student, including one without a bot profile (attendance:write)",
)
async def attendance(request: Request, public_id: str, body: AttendanceInput, ctx: Ctx):
    async def action(api, client, actor):
        result = await AttendanceService(
            api.session, api.settings.verification_secret
        ).mark_student(actor, public_id, body.full_name, body.time_login, body.telegram_tag)
        event = await EventService(api.session)._event(public_id)
        record = await api.session.get(Attendance, (event.id, as_uuid(result["person_id"])))
        return {
            "event_id": public_id,
            "person_id": result["person_id"],
            "attended": True,
            "already_marked": not result["created"],
            "registered_in_bot_at_checkin": record.was_bot_registered,
        }

    return await mutate(
        request, ctx, "attendance:write", body.model_dump(), action, event_id=public_id
    )


@router.post(
    "/events/{public_id}/attendance-imports",
    summary="Bounded multipart XLSX preview; no attendance is written (attendance:write)",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {"file": {"type": "string", "format": "binary"}},
                    }
                }
            },
        }
    },
)
async def attendance_import(request: Request, public_id: str, ctx: Ctx):
    from starlette.datastructures import UploadFile

    try:
        async with request.form(max_files=1, max_fields=0) as form:
            upload = form.get("file")
            if not isinstance(upload, UploadFile) or not (upload.filename or "").lower().endswith(
                ".xlsx"
            ):
                raise ApiError(422, "XLSX_REQUIRED", "Upload one .xlsx file named file")
            content = await upload.read(MAX_INPUT_BYTES + 1)
    except ApiError:
        raise
    except Exception:
        raise ApiError(422, "INVALID_MULTIPART", "Upload one .xlsx file") from None
    if len(content) > MAX_INPUT_BYTES:
        raise ApiError(413, "FILE_TOO_LARGE", "File exceeds 10 MB")
    try:
        parsed = await asyncio.to_thread(parse_attendance_xlsx, content)
    except ValueError as error:
        raise ApiError(422, "INVALID_XLSX", str(error)) from None

    async def action(api, client, actor):
        return await AttendanceService(
            api.session, api.settings.verification_secret
        ).preview_import(actor, public_id, parsed["rows"], parsed.get("issues"))

    return await mutate(
        request,
        ctx,
        "attendance:write",
        {"file_sha256": hashlib.sha256(content).hexdigest()},
        action,
        event_id=public_id,
        status=201,
    )


async def import_mutation(request, ctx, batch_id, body, *, resolve=False):
    async def action(api, client, actor):
        batch = await api.session.get(AttendanceImport, as_uuid(batch_id))
        if not batch:
            raise ApiError(404, "NOT_FOUND", "Import not found")
        event = await api.session.get(Event, batch.event_id)
        await api.event_access(client, event.public_id, lock=True)
        service = AttendanceService(api.session, api.settings.verification_secret)
        if resolve:
            return await service.resolve_import(actor, batch_id, body.rows, body.version)
        return await service.confirm_import(actor, batch_id, body.version)

    return await mutate(request, ctx, "attendance:write", body.model_dump(), action)


@router.post(
    "/attendance-imports/{batch_id}/confirm",
    summary="Confirm current valid rows idempotently (attendance:write)",
)
async def confirm_import(request: Request, batch_id: str, body: ConfirmImportInput, ctx: Ctx):
    return await import_mutation(request, ctx, batch_id, body)


@router.post(
    "/attendance-imports/{batch_id}/resolve",
    summary="Explicitly accept selected FIO conflicts (attendance:write)",
)
async def resolve_import(request: Request, batch_id: str, body: ResolveImportInput, ctx: Ctx):
    return await import_mutation(request, ctx, batch_id, body, resolve=True)


@router.get("/changes", summary="Durable club-scoped changes in commit order (changes:read)")
async def changes(
    request: Request,
    ctx: Ctx,
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    async with authorized(request, ctx, "changes:read") as (api, client):
        return await api.changes(client, cursor, limit)


@router.post(
    "/webhook-endpoints",
    summary="Register public HTTPS endpoint; signing secret returned once (changes:read)",
)
async def endpoint(request: Request, body: EndpointInput, ctx: Ctx):
    return await mutate(
        request,
        ctx,
        "changes:read",
        body.model_dump(),
        lambda api, client, actor: api.endpoint(client, body.url, body.event_types),
        status=201,
    )


@router.delete("/webhook-endpoints/{endpoint_id}", summary="Stop a webhook endpoint (changes:read)")
async def delete_endpoint(request: Request, endpoint_id: str, ctx: Ctx):
    async def action(api, client, actor):
        endpoint = await api.session.get(ApiEndpoint, as_uuid(endpoint_id), with_for_update=True)
        if not endpoint or endpoint.client_id != client.id:
            raise ApiError(404, "NOT_FOUND", "Endpoint not found")
        from cu_clubs.api_service import now

        endpoint.revoked_at = now()
        return {"id": endpoint.id.hex, "active": False}

    return await mutate(request, ctx, "changes:read", {}, action)


@router.get("/webhook-endpoints", summary="Own endpoints without signing secrets (changes:read)")
async def endpoints(request: Request, ctx: Ctx):
    async with authorized(request, ctx, "changes:read") as (api, client):
        rows = await api.session.scalars(
            select(ApiEndpoint)
            .where(ApiEndpoint.client_id == client.id)
            .order_by(ApiEndpoint.created_at)
            .limit(100)
        )
        return {
            "items": [
                {
                    "id": row.id.hex,
                    "url": row.url,
                    "event_types": row.event_types,
                    "active": row.revoked_at is None,
                }
                for row in rows
            ]
        }


@router.get(
    "/webhook-deliveries", summary="Own delivery status, safe errors and attempts (changes:read)"
)
async def deliveries(
    request: Request,
    ctx: Ctx,
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    async with authorized(request, ctx, "changes:read") as (api, client):
        rows = list(
            await api.session.scalars(
                select(ApiDelivery)
                .join(ApiEndpoint)
                .where(ApiEndpoint.client_id == client.id)
                .order_by(ApiDelivery.id)
                .offset(cursor)
                .limit(limit + 1)
            )
        )
        return {
            "items": [
                {
                    "id": row.id.hex,
                    "status": row.status,
                    "attempts": row.attempts,
                    "last_error": row.last_error,
                    "change_id": row.change_id,
                    "next_attempt_at": row.available_at.isoformat()
                    if row.status == "pending"
                    else None,
                }
                for row in rows[:limit]
            ],
            "next_cursor": cursor + limit if len(rows) > limit else None,
        }


@router.post(
    "/webhook-deliveries/{delivery_id}/retry",
    summary="Retry failed delivery using the same delivery ID (changes:read)",
)
async def retry_delivery(request: Request, delivery_id: str, ctx: Ctx):
    async def action(api, client, actor):
        row = await api.session.get(ApiDelivery, as_uuid(delivery_id), with_for_update=True)
        endpoint = await api.session.get(ApiEndpoint, row.endpoint_id) if row else None
        if not endpoint or endpoint.client_id != client.id or endpoint.revoked_at is not None:
            raise ApiError(404, "NOT_FOUND", "Delivery not found")
        if row.status not in {"failed", "cancelled"}:
            raise ApiError(409, "DELIVERY_NOT_FAILED", "Only failed deliveries may be retried")
        from cu_clubs.api_service import now

        row.status, row.available_at, row.lease_until, row.last_error = "pending", now(), None, None
        return {"id": row.id.hex, "status": "pending"}

    return await mutate(request, ctx, "changes:read", {}, action)


def install_api(app, settings):
    app.state.settings = settings
    response_types = {
        "event": EventOutput,
        "create_event": EventOutput,
        "edit_event": EventOutput,
        "publish_event": EventOutput,
        "invite": EventOutput,
        "cancel_event": EventOutput,
        "create_link": LinkOutput,
        "exchange": TokenOutput,
    }
    for route in router.routes:
        if not hasattr(route, "methods"):
            continue
        if route.name in {"create_event", "create_link", "attendance_import", "endpoint"}:
            route.status_code = 201
        if route.name in response_types:
            # Response is returned explicitly; this model documents the wire contract.
            route.response_model = response_types[route.name]
            from fastapi.utils import create_model_field

            route.response_field = create_model_field(
                name="Response_" + route.name, type_=route.response_model, mode="serialization"
            )
        extra = dict(route.openapi_extra or {})
        parameters = [
            item
            for item in extra.get("parameters", [])
            if item.get("name") not in {"Idempotency-Key", "If-Match"}
        ]
        if route.methods & {"POST", "PUT", "PATCH", "DELETE"}:
            parameters.append(
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "minLength": 1, "maxLength": 128},
                    "description": "Reuse only for retries of the same operation and body.",
                }
            )
        if route.name in {"edit_event", "publish_event", "invite", "cancel_event"}:
            parameters.append(
                {
                    "name": "If-Match",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string"},
                    "description": 'Current ETag, for example "3".',
                }
            )
        extra["parameters"] = parameters
        route.openapi_extra = extra
    app.include_router(router)

    def error_response(request, error):
        request_id = getattr(request.state, "request_id", uuid4().hex)
        payload = {"code": error.code, "message": error.message}
        if error.field:
            payload["field"] = error.field
        headers = {"X-Request-ID": request_id}
        if error.status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        if error.status == 429:
            headers["Retry-After"] = "60"
        return JSONResponse(
            {"error": payload, "request_id": request_id}, status_code=error.status, headers=headers
        )

    @app.exception_handler(ApiError)
    async def api_error(request, error):
        return error_response(request, error)

    @app.exception_handler(ValidationError)
    async def invalid_domain(request, error):
        message = str(error)
        code, status = (
            ("EVENT_FULL", 409)
            if "места заняты" in message
            else ("REGISTRATION_CLOSED", 409)
            if "Регистрация завершена" in message
            else ("INVALID_STATE", 422)
        )
        return error_response(request, ApiError(status, code, message))

    @app.exception_handler(PermissionDenied)
    async def forbidden(request, error):
        return error_response(request, ApiError(403, "FORBIDDEN", str(error)))

    @app.exception_handler(EventNotFound)
    @app.exception_handler(ClubNotFound)
    async def missing(request, error):
        return error_response(request, ApiError(404, "NOT_FOUND", "Resource not found"))

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, error):
        field = ".".join(str(x) for x in error.errors()[0]["loc"]) if error.errors() else None
        return error_response(
            request, ApiError(422, "VALIDATION_ERROR", "Invalid request field", field=field)
        )

    @app.middleware("http")
    async def api_limits(request, call_next):
        if not request.url.path.startswith("/api/v1"):
            return await call_next(request)
        request.state.request_id = uuid4().hex
        limit = (
            MAX_INPUT_BYTES + 65536
            if request.url.path.endswith("/attendance-imports")
            else 256 * 1024
        )
        content = bytearray()
        try:
            async with asyncio.timeout(30):
                async for part in request.stream():
                    content.extend(part)
                    if len(content) > limit:
                        return error_response(
                            request, ApiError(413, "BODY_TOO_LARGE", "Request body is too large")
                        )
        except TimeoutError:
            return error_response(
                request, ApiError(408, "REQUEST_TIMEOUT", "Request body timed out")
            )
        request._body = bytes(content)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        return response
