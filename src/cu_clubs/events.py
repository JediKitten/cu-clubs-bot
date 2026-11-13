"""Shared meeting commands. Callers own transactions; delivery uses the outbox."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cu_clubs.event_models import (
    Event,
    EventClub,
    EventInvitation,
    EventTemplate,
    Registration,
    RegistrationHistory,
)
from cu_clubs.models import Audit, Club, Membership, Outbox, Person, Subscription, TelegramAccount
from cu_clubs.services import ClubService, PermissionDenied, ValidationError


class EventNotFound(ValidationError):
    pass


EDIT_FIELDS = {
    "title",
    "description",
    "post_registration_text",
    "starts_at",
    "ends_at",
    "registration_deadline",
    "capacity",
    "expected_attendance",
    "venue_type",
    "room",
    "venue_details",
    "needs_talk",
    "needs_microphones",
    "visibility",
}
DATE_FIELDS = {"starts_at", "ends_at", "registration_deadline"}
TEMPLATE_FIELDS = EDIT_FIELDS - DATE_FIELDS - {"room"}


def utcnow() -> datetime:
    return datetime.now(UTC)


def _date(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise ValidationError("Укажи корректные дату и время.") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("Дата должна содержать часовой пояс.")
    return value.astimezone(UTC)


def validate_fields(fields: dict, current: Event | None = None, *, complete=False) -> dict:
    """Validate the entire candidate before mutating a persistent meeting."""
    if fields.keys() - EDIT_FIELDS:
        raise ValidationError("Неизвестное поле встречи.")
    candidate = (
        {key: getattr(current, key) for key in EDIT_FIELDS}
        if current
        else {
            "title": "",
            "description": "",
            "post_registration_text": "",
            "starts_at": None,
            "ends_at": None,
            "registration_deadline": None,
            "capacity": None,
            "expected_attendance": None,
            "venue_type": "campus",
            "room": None,
            "venue_details": "",
            "needs_talk": False,
            "needs_microphones": False,
            "visibility": "open",
        }
    )
    candidate.update(fields)
    for key in DATE_FIELDS:
        candidate[key] = _date(candidate[key])
    for key, limit in {
        "title": 45,
        "description": 250,
        "post_registration_text": 150,
        "venue_details": 2000,
    }.items():
        value = candidate[key]
        if not isinstance(value, str) or len(value.strip()) > limit:
            raise ValidationError(f"Некорректное значение поля {key}; максимум {limit} символов.")
        candidate[key] = value.strip()
    if candidate["title"] and len(candidate["title"]) < 5:
        raise ValidationError("Название мероприятия должно содержать от 5 до 45 символов.")
    if candidate["post_registration_text"] and len(candidate["post_registration_text"]) < 10:
        raise ValidationError("Текст должен содержать от 10 до 150 символов.")
    if candidate["room"] is not None:
        if not isinstance(candidate["room"], str) or len(candidate["room"].strip()) > 300:
            raise ValidationError("Место должно быть текстом до 300 символов.")
        candidate["room"] = candidate["room"].strip() or None
    for key in ("capacity", "expected_attendance"):
        value = candidate[key]
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 1_000_000
        ):
            raise ValidationError("Количество участников должно быть положительным числом.")
    for key in ("needs_talk", "needs_microphones"):
        if not isinstance(candidate[key], bool):
            raise ValidationError("Для техники выбери «Да» или «Нет».")
    if candidate["visibility"] not in {"open", "closed"}:
        raise ValidationError("Выбери открытую или закрытую встречу.")
    if candidate["venue_type"] not in {"campus", "external", "online"}:
        raise ValidationError("Неизвестный тип площадки.")
    start, end, deadline = (
        candidate[key] for key in ("starts_at", "ends_at", "registration_deadline")
    )
    if start and end and end <= start:
        raise ValidationError("Окончание должно быть позже начала встречи.")
    if start and deadline and deadline > start:
        raise ValidationError("Запись должна закончиться не позже начала встречи.")
    if complete:
        if not candidate["title"] or not candidate["description"] or not start or not end:
            raise ValidationError("Заполни название, описание, начало и окончание встречи.")
        if candidate["venue_type"] != "campus" and not candidate["venue_details"]:
            raise ValidationError("Укажи адрес площадки или инструкцию подключения онлайн.")
        if deadline is None:
            candidate["registration_deadline"] = start
    return candidate


class EventService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.clubs = ClubService(session)

    async def _event(self, public_id: str, *, lock=False) -> Event:
        query = (
            select(Event)
            .where(Event.public_id == public_id)
            .execution_options(populate_existing=True)
        )
        if lock:
            query = query.with_for_update()
        event = await self.session.scalar(query)
        if event is None:
            raise EventNotFound("Встреча не найдена.")
        return event

    async def _event_clubs(self, event: Event) -> list[Club]:
        return list(
            await self.session.scalars(
                select(Club)
                .join(EventClub, EventClub.club_id == Club.id)
                .where(EventClub.event_id == event.id)
                .order_by(EventClub.role, Club.name)
            )
        )

    async def _can_manage(self, actor_id: int | None, event: Event, *, leaders_only=False) -> bool:
        if actor_id is None:
            return False
        if await self.clubs.is_admin(actor_id):
            return True
        for club in await self._event_clubs(event):
            permissions = await self.clubs.permissions(actor_id, club.public_id)
            if permissions["is_leader"] if leaders_only else permissions["manage_events"]:
                return True
        return False

    async def _manage(self, actor_id: int, event: Event, *, leaders_only=False) -> None:
        if not await self._can_manage(actor_id, event, leaders_only=leaders_only):
            raise PermissionDenied("Нет права управлять этой встречей.")

    async def _person(self, actor_id: int) -> Person:
        account = await self.clubs._account(actor_id)
        if account is None:
            raise PermissionDenied("Сначала открой бота и заполни профиль.")
        return account.person

    async def _registration(self, event: Event, actor_id: int | None) -> Registration | None:
        if actor_id is None:
            return None
        account = await self.clubs._account(actor_id)
        if account is None:
            return None
        return await self.session.scalar(
            select(Registration)
            .where(Registration.event_id == event.id, Registration.person_id == account.person_id)
            .execution_options(populate_existing=True)
        )

    async def _count(self, event: Event) -> int:
        return (
            await self.session.scalar(
                select(func.count())
                .select_from(Registration)
                .where(Registration.event_id == event.id, Registration.status == "active")
            )
            or 0
        )

    async def _view(self, event: Event, actor_id: int | None, via_link: bool) -> None:
        if await self._can_manage(actor_id, event):
            return
        registration = await self._registration(event, actor_id)
        if registration is not None and registration.status == "active":
            return
        if event.status in {"published", "held", "cancelled"} and (
            event.visibility == "open" or via_link
        ):
            return
        if actor_id is not None and event.status in {"published", "held", "cancelled"}:
            for club in await self._event_clubs(event):
                if (await self.clubs.permissions(actor_id, club.public_id))["record_attendance"]:
                    return
        # Partner leaders may review pending invitations, without gaining edit rights.
        if event.status == "pending_partner" and actor_id is not None:
            invitations = await self.session.scalars(
                select(EventInvitation).where(
                    EventInvitation.event_id == event.id,
                    EventInvitation.status == "pending",
                    EventInvitation.version == event.version,
                )
            )
            for invitation in invitations:
                partner = await self.session.get(Club, invitation.partner_club_id)
                permissions = await self.clubs.permissions(actor_id, partner.public_id)
                if permissions["is_admin"] or permissions["is_leader"]:
                    return
        raise EventNotFound("Встреча не найдена.")

    async def _dto(self, event: Event, actor_id: int | None = None) -> dict:
        clubs = await self._event_clubs(event)
        registration = await self._registration(event, actor_id)
        result = {key: getattr(event, key) for key in EDIT_FIELDS}
        result.update(
            id=str(event.id),
            public_id=event.public_id,
            status=event.status,
            version=event.version,
            clubs=[
                {"public_id": c.public_id, "name": c.name, "level": c.level_name} for c in clubs
            ],
            initiator_club_id=str(event.initiator_club_id),
            registered_count=await self._count(event),
            registered=bool(registration and registration.status == "active"),
            registered_at=registration.registered_at.isoformat() if registration else None,
            can_manage=await self._can_manage(actor_id, event),
            can_reschedule=await self._can_manage(actor_id, event, leaders_only=True),
            start_registration_snapshot=event.start_registration_snapshot,
            last_editor_telegram_id=event.last_editor_telegram_id,
            created_at=event.created_at.isoformat(),
            updated_at=event.updated_at.isoformat(),
        )
        for key in DATE_FIELDS:
            result[key] = result[key].isoformat() if result[key] else None
        return result

    def _audit(self, actor_id: int, action: str, event: Event, details: dict | None = None):
        self.session.add(
            Audit(
                actor_telegram_id=actor_id,
                action=action,
                entity_id=event.id,
                details=details or {"version": event.version},
            )
        )

    @staticmethod
    def _version(event: Event, expected_version: int) -> None:
        if type(expected_version) is not int or expected_version != event.version:
            raise ValidationError(
                "Карточку уже изменил другой организатор. "
                "Открой актуальную версию и внеси свою правку."
            )

    async def _notify(self, event: Event, kind: str, *, subscribers=False, extra=None) -> None:
        club_ids = [club.id for club in await self._event_clubs(event)]
        predicates = [Membership.club_id.in_(club_ids), Membership.role == "leader"]
        leader_ids = set(
            await self.session.scalars(
                select(TelegramAccount.telegram_id)
                .join(Membership, Membership.person_id == TelegramAccount.person_id)
                .where(*predicates)
            )
        )
        participant_ids = set(
            await self.session.scalars(
                select(TelegramAccount.telegram_id)
                .join(Registration, Registration.person_id == TelegramAccount.person_id)
                .where(Registration.event_id == event.id, Registration.status == "active")
            )
        )
        recipients = leader_ids | participant_ids
        if subscribers and event.visibility == "open":
            recipients |= set(
                await self.session.scalars(
                    select(TelegramAccount.telegram_id)
                    .join(Subscription, Subscription.person_id == TelegramAccount.person_id)
                    .where(Subscription.club_id.in_(club_ids))
                )
            )
        for recipient in recipients:
            self.session.add(
                Outbox(
                    kind=kind,
                    recipient_telegram_id=recipient,
                    payload={"event": event.public_id, "version": event.version, **(extra or {})},
                    dedupe_key=f"{kind}:{event.id}:{event.version}:{recipient}",
                )
            )

    async def _capture_start(self, event: Event) -> None:
        if (
            event.start_registration_snapshot is None
            and event.starts_at is not None
            and utcnow() >= event.starts_at
            and event.status in {"published", "held"}
        ):
            # Every registration transition also locks the event and captures this first.
            # Therefore this equals the active set at the original start, even when no
            # periodic worker ran at that exact instant.
            people = await self.session.scalars(
                select(Registration.person_id)
                .where(Registration.event_id == event.id, Registration.status == "active")
                .order_by(Registration.person_id)
            )
            event.start_registration_snapshot = [str(person_id) for person_id in people]
            event.snapshot_starts_at = event.starts_at

    async def capture_started_events(self) -> int:
        events = list(
            await self.session.scalars(
                select(Event)
                .where(
                    Event.status.in_(["published", "held"]),
                    Event.starts_at <= utcnow(),
                    Event.start_registration_snapshot.is_(None),
                )
                .with_for_update(skip_locked=True)
            )
        )
        for event in events:
            await self._capture_start(event)
        return len(events)

    async def create_draft(
        self, actor_id: int, club_public_id: str, fields: dict | None = None
    ) -> dict:
        await self.clubs.authorize(actor_id, club_public_id, "manage_events")
        club = await self.clubs._club(club_public_id)
        person = await self._person(actor_id)
        candidate = validate_fields(fields or {})
        event = Event(
            initiator_club_id=club.id,
            author_person_id=person.id,
            last_editor_telegram_id=actor_id,
            **candidate,
        )
        self.session.add(event)
        await self.session.flush()
        self.session.add(EventClub(event_id=event.id, club_id=club.id, role="initiator"))
        self._audit(actor_id, "event.draft_created", event)
        await self.session.flush()
        return await self._dto(event, actor_id)

    async def update_draft(
        self, actor_id: int, event_public_id: str, fields: dict, expected_version: int
    ) -> dict:
        event = await self._event(event_public_id, lock=True)
        await self._manage(actor_id, event)
        self._version(event, expected_version)
        if event.status not in {"draft", "pending_partner"}:
            raise ValidationError("Эта встреча уже опубликована. Открой редактирование встречи.")
        candidate = validate_fields(fields, event, complete=event.status == "pending_partner")
        old = await self.session.scalar(
            select(EventInvitation).where(
                EventInvitation.event_id == event.id, EventInvitation.status == "pending"
            )
        )
        for key, value in candidate.items():
            setattr(event, key, value)
        event.version += 1
        event.last_editor_telegram_id = actor_id
        if old:
            old.status = "superseded"
            await self.session.flush()
            await self._new_invitation(event, old.partner_club_id)
        self._audit(actor_id, "event.draft_updated", event)
        await self.session.flush()
        return await self._dto(event, actor_id)

    async def publish(self, actor_id: int, event_public_id: str, expected_version: int) -> dict:
        event = await self._event(event_public_id, lock=True)
        await self._manage(actor_id, event)
        if event.status == "published":
            return await self._dto(event, actor_id)
        self._version(event, expected_version)
        if event.status != "draft":
            raise ValidationError("Сначала дождись решения партнёра или отмени приглашение.")
        candidate = validate_fields({}, event, complete=True)
        for key, value in candidate.items():
            setattr(event, key, value)
        event.status = "published"
        event.version += 1
        event.last_editor_telegram_id = actor_id
        self._audit(actor_id, "event.published", event)
        await self.session.flush()
        await self._notify(event, "event.published", subscribers=True)
        return await self._dto(event, actor_id)

    async def get_event(
        self, event_public_id: str, actor_id: int | None = None, *, via_link=False
    ) -> dict:
        event = await self._event(event_public_id)
        await self._view(event, actor_id, via_link)
        return await self._dto(event, actor_id)

    async def public_events(
        self, start: datetime, end: datetime, club_public_id: str | None = None
    ) -> list[dict]:
        start, end = _date(start), _date(end)
        if not start or not end or start >= end:
            raise ValidationError("Некорректный период календаря.")
        query = select(Event).where(
            Event.status.in_(["published", "held"]),
            Event.visibility == "open",
            Event.starts_at >= start,
            Event.starts_at < end,
        )
        if club_public_id:
            club = await self.clubs._club(club_public_id)
            query = query.join(EventClub).where(EventClub.club_id == club.id)
        events = await self.session.scalars(query.order_by(Event.starts_at, Event.public_id))
        return [await self._dto(event) for event in events]

    async def managed_events(self, actor_id: int, club_public_id: str) -> list[dict]:
        permissions = await self.clubs.permissions(actor_id, club_public_id)
        if not (permissions["manage_events"] or permissions["record_attendance"]):
            raise PermissionDenied("Нет доступа к встречам этого клуба.")
        club = await self.clubs._club(club_public_id)
        query = select(Event).join(EventClub).where(EventClub.club_id == club.id)
        if not permissions["manage_events"]:
            query = query.where(Event.status.in_(["published", "held"]))
        events = await self.session.scalars(query.order_by(Event.starts_at.desc().nullsfirst()))
        return [await self._dto(event, actor_id) for event in events]

    async def my_events(self, actor_id: int) -> list[dict]:
        person = await self._person(actor_id)
        events = await self.session.scalars(
            select(Event)
            .join(Registration)
            .where(Registration.person_id == person.id, Registration.status == "active")
            .order_by(Event.starts_at)
        )
        return [await self._dto(event, actor_id) for event in events]

    async def register(
        self, actor_id: int, event_public_id: str, *, via_link=False, source="telegram"
    ) -> dict:
        if source not in {"telegram", "api"}:
            raise ValidationError("Некорректный источник регистрации.")
        event = await self._event(event_public_id, lock=True)
        await self._view(event, actor_id, via_link)
        person = await self._person(actor_id)
        if person.kind != "student" or person.verified_at is None or not person.time_login:
            raise PermissionDenied("Подтверди студенческую почту, чтобы записаться.")
        registration = await self._registration(event, actor_id)
        if registration and registration.status == "active":
            return await self._dto(event, actor_id)
        await self._capture_start(event)
        now = utcnow()
        if event.status != "published" or event.starts_at is None or now >= event.starts_at:
            raise ValidationError("Регистрация завершена.")
        if event.registration_deadline and now >= event.registration_deadline:
            raise ValidationError("Регистрация завершена.")
        if event.capacity is not None and await self._count(event) >= event.capacity:
            raise ValidationError("Все места заняты.")
        if registration is None:
            registration = Registration(event_id=event.id, person_id=person.id, source=source)
            self.session.add(registration)
        else:
            registration.status = "active"
            registration.registered_at = now
            registration.cancelled_at = None
            registration.source = source
        self.session.add(
            RegistrationHistory(
                event_id=event.id, person_id=person.id, status="active", changed_at=now
            )
        )
        self._audit(actor_id, "event.registered", event, {"person": str(person.id)})
        await self.session.flush()
        return await self._dto(event, actor_id)

    async def cancel_registration(
        self, actor_id: int, event_public_id: str, *, expected_registered_at: str | None = None
    ) -> dict:
        event = await self._event(event_public_id, lock=True)
        registration = await self._registration(event, actor_id)
        if registration is None:
            raise EventNotFound("Запись на встречу не найдена.")
        if (
            expected_registered_at is not None
            and _date(expected_registered_at) != registration.registered_at
        ):
            raise ValidationError("Запись изменилась. Открой актуальную карточку встречи.")
        if registration.status == "active":
            await self._capture_start(event)
            registration.status = "cancelled"
            registration.cancelled_at = utcnow()
            self.session.add(
                RegistrationHistory(
                    event_id=event.id,
                    person_id=registration.person_id,
                    status="cancelled",
                    changed_at=registration.cancelled_at,
                )
            )
            self._audit(actor_id, "event.registration_cancelled", event)
            await self.session.flush()
        return await self._dto(event, actor_id)

    async def update_event(
        self, actor_id: int, event_public_id: str, fields: dict, expected_version: int
    ) -> dict:
        event = await self._event(event_public_id, lock=True)
        await self._manage(actor_id, event)
        self._version(event, expected_version)
        if event.status != "published":
            raise ValidationError("Редактирование доступно для опубликованной встречи.")
        if fields.keys() & {"starts_at", "ends_at", "registration_deadline"}:
            await self._manage(actor_id, event, leaders_only=True)
        candidate = validate_fields(fields, event, complete=True)
        count = await self._count(event)
        if candidate["capacity"] is not None and candidate["capacity"] < count:
            raise ValidationError("Нельзя уменьшить лимит ниже числа записанных.")
        await self._capture_start(event)
        before = {
            key: getattr(event, key).isoformat()
            if key in DATE_FIELDS and getattr(event, key)
            else getattr(event, key)
            for key in fields
        }
        for key, value in candidate.items():
            setattr(event, key, value)
        event.version += 1
        event.last_editor_telegram_id = actor_id
        self._audit(actor_id, "event.updated", event, {"version": event.version, "before": before})
        await self.session.flush()
        await self._notify(event, "event.changed")
        return await self._dto(event, actor_id)

    async def cancel_event(
        self, actor_id: int, event_public_id: str, expected_version: int
    ) -> dict:
        event = await self._event(event_public_id, lock=True)
        await self._manage(actor_id, event)
        if event.status == "cancelled":
            return await self._dto(event, actor_id)
        self._version(event, expected_version)
        if event.status != "published":
            raise ValidationError("Отменить можно опубликованную встречу.")
        await self._capture_start(event)
        event.status = "cancelled"
        event.version += 1
        event.last_editor_telegram_id = actor_id
        self._audit(actor_id, "event.cancelled", event)
        await self.session.flush()
        await self._notify(event, "event.cancelled")
        return await self._dto(event, actor_id)

    async def _new_invitation(self, event: Event, partner_id: UUID) -> EventInvitation:
        invitation = EventInvitation(
            event_id=event.id, partner_club_id=partner_id, version=event.version
        )
        self.session.add(invitation)
        await self.session.flush()
        recipients = await self.session.scalars(
            select(TelegramAccount.telegram_id)
            .join(Membership, Membership.person_id == TelegramAccount.person_id)
            .where(Membership.club_id == partner_id, Membership.role == "leader")
        )
        for recipient in recipients:
            self.session.add(
                Outbox(
                    kind="event.invitation",
                    recipient_telegram_id=recipient,
                    payload={
                        "event": event.public_id,
                        "version": event.version,
                        "invitation": str(invitation.id),
                    },
                    dedupe_key=f"event.invitation:{invitation.id}:{recipient}",
                )
            )
        return invitation

    async def invite_partner(
        self,
        actor_id: int,
        event_public_id: str,
        partner_club_public_id: str,
        expected_version: int,
    ) -> dict:
        event = await self._event(event_public_id, lock=True)
        await self._manage(actor_id, event)
        self._version(event, expected_version)
        if event.status != "draft":
            raise ValidationError("Приглашение можно отправить из черновика.")
        candidate = validate_fields({}, event, complete=True)
        partner = await self.clubs._club(partner_club_public_id)
        if partner.id == event.initiator_club_id:
            raise ValidationError("Выбери другой клуб для коллаборации.")
        if not await self.session.scalar(
            select(Membership.person_id).where(
                Membership.club_id == partner.id, Membership.role == "leader"
            )
        ):
            raise ValidationError("У клуба-партнёра ещё не назначен лидер.")
        for key, value in candidate.items():
            setattr(event, key, value)
        event.status = "pending_partner"
        event.version += 1
        invitation = await self._new_invitation(event, partner.id)
        self._audit(actor_id, "event.partner_invited", event, {"partner": str(partner.id)})
        dto = await self._dto(event, actor_id)
        dto["invitation_id"] = str(invitation.id)
        return dto

    async def pending_invitations(self, actor_id: int) -> list[dict]:
        result = []
        rows = await self.session.execute(
            select(EventInvitation, Club)
            .join(Club, Club.id == EventInvitation.partner_club_id)
            .where(EventInvitation.status == "pending")
        )
        for invitation, partner in rows:
            permissions = await self.clubs.permissions(actor_id, partner.public_id)
            if permissions["is_leader"] or permissions["is_admin"]:
                event = await self.session.get(Event, invitation.event_id)
                if event.status == "pending_partner" and event.version == invitation.version:
                    dto = await self._dto(event, actor_id)
                    dto["invitation_id"] = str(invitation.id)
                    dto["partner_club"] = partner.public_id
                    result.append(dto)
        return result

    async def respond_invitation(self, actor_id: int, invitation_id: str, accept: bool) -> dict:
        try:
            invitation_key = UUID(invitation_id)
        except (ValueError, TypeError):
            raise ValidationError("Приглашение не найдено.") from None
        # All meeting writes lock the event first, including invitation decisions.
        invitation = await self.session.get(EventInvitation, invitation_key)
        if invitation is None:
            raise ValidationError("Приглашение не найдено.")
        event_id = await self.session.scalar(
            select(Event.public_id).where(Event.id == invitation.event_id)
        )
        event = await self._event(event_id, lock=True)
        await self.session.refresh(invitation)
        partner = await self.session.get(Club, invitation.partner_club_id)
        permissions = await self.clubs.permissions(actor_id, partner.public_id)
        if not permissions["is_leader"]:
            raise PermissionDenied("Решение может принять только лидер клуба-партнёра.")
        if not isinstance(accept, bool):
            raise ValidationError("Нужно принять или отклонить приглашение.")
        if invitation.status in {"accepted", "declined"}:
            return await self._dto(event, actor_id)
        if (
            event.status != "pending_partner"
            or invitation.status != "pending"
            or invitation.version != event.version
        ):
            raise ValidationError("Приглашение устарело. Открой актуальную карточку.")
        invitation.status = "accepted" if accept else "declined"
        invitation.decided_by_telegram_id = actor_id
        invitation.decided_at = utcnow()
        event.version += 1
        event.status = "published" if accept else "draft"
        if accept:
            self.session.add(EventClub(event_id=event.id, club_id=partner.id, role="partner"))
        self._audit(
            actor_id, "event.partner_accepted" if accept else "event.partner_declined", event
        )
        await self.session.flush()
        await self._notify(
            event, "event.published" if accept else "event.partner_declined", subscribers=accept
        )
        return await self._dto(event, actor_id)

    async def withdraw_invitation(
        self, actor_id: int, event_public_id: str, expected_version: int
    ) -> dict:
        event = await self._event(event_public_id, lock=True)
        await self._manage(actor_id, event)
        self._version(event, expected_version)
        if event.status != "pending_partner":
            raise ValidationError("Нет ожидающего приглашения.")
        invitations = await self.session.scalars(
            select(EventInvitation).where(
                EventInvitation.event_id == event.id, EventInvitation.status == "pending"
            )
        )
        for invitation in invitations:
            invitation.status = "superseded"
        event.status = "draft"
        event.version += 1
        self._audit(actor_id, "event.invitation_withdrawn", event)
        await self.session.flush()
        return await self._dto(event, actor_id)

    async def save_template(self, actor_id: int, event_public_id: str, name: str) -> dict:
        event = await self._event(event_public_id)
        await self._manage(actor_id, event)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 150:
            raise ValidationError("Название шаблона должно содержать от 1 до 150 символов.")
        # Templates remain under the initiating club, whose management is required.
        club = await self.session.get(Club, event.initiator_club_id)
        await self.clubs.authorize(actor_id, club.public_id, "manage_events")
        if await self.session.scalar(
            select(EventTemplate.id).where(
                EventTemplate.club_id == club.id, EventTemplate.name == name.strip()
            )
        ):
            raise ValidationError("Шаблон с таким названием уже существует.")
        fields = {key: getattr(event, key) for key in TEMPLATE_FIELDS}
        if event.starts_at and event.ends_at:
            fields["duration_minutes"] = int(
                (event.ends_at - event.starts_at).total_seconds() // 60
            )
        template = EventTemplate(club_id=club.id, name=name.strip(), fields=fields)
        self.session.add(template)
        await self.session.flush()
        return {
            "public_id": template.public_id,
            "name": template.name,
            "fields": template.fields,
            "version": template.version,
        }

    async def templates(self, actor_id: int, club_public_id: str) -> list[dict]:
        await self.clubs.authorize(actor_id, club_public_id, "manage_events")
        club = await self.clubs._club(club_public_id)
        rows = await self.session.scalars(
            select(EventTemplate)
            .where(EventTemplate.club_id == club.id, EventTemplate.deleted_at.is_(None))
            .order_by(EventTemplate.name)
        )
        return [
            {"public_id": t.public_id, "name": t.name, "fields": t.fields, "version": t.version}
            for t in rows
        ]

    async def from_template(self, actor_id: int, template_public_id: str) -> dict:
        template = await self.session.scalar(
            select(EventTemplate).where(
                EventTemplate.public_id == template_public_id, EventTemplate.deleted_at.is_(None)
            )
        )
        if template is None:
            raise ValidationError("Шаблон не найден.")
        club = await self.session.get(Club, template.club_id)
        return await self.create_draft(
            actor_id, club.public_id, {k: v for k, v in template.fields.items() if k in EDIT_FIELDS}
        )

    async def delete_template(
        self, actor_id: int, template_public_id: str, expected_version: int
    ) -> None:
        template = await self.session.scalar(
            select(EventTemplate)
            .where(EventTemplate.public_id == template_public_id)
            .with_for_update()
        )
        if template is None:
            raise ValidationError("Шаблон не найден.")
        club = await self.session.get(Club, template.club_id)
        await self.clubs.authorize(actor_id, club.public_id, "manage_events")
        if template.version != expected_version:
            raise ValidationError("Шаблон изменился. Открой актуальную версию.")
        template.deleted_at = utcnow()
        template.version += 1
