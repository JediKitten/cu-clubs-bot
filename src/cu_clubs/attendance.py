"""Shared attendance commands. Callers commit transactions; tokens never expire by time."""

import base64
import hashlib
import hmac
from datetime import UTC, datetime
from importlib.resources import files
from io import BytesIO
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import qrcode
from aiogram.types import BufferedInputFile
from PIL import Image
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from cu_clubs import legacy
from cu_clubs.attendance_models import Attendance, AttendanceImport, AttendanceQR, AttendanceQRGrant
from cu_clubs.event_models import Event, Registration
from cu_clubs.events import EventService
from cu_clubs.identity import normalize_login, normalize_name
from cu_clubs.models import Audit, Person, TelegramAccount
from cu_clubs.services import ClubService, PermissionDenied, ValidationError


def now():
    return datetime.now(UTC)


def _key(secret):
    value = secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret
    if not value or len(value) < 32:
        raise ValidationError("QR временно недоступны: ключ не настроен")
    return value.encode()


def _raw(token, secret):
    signature = hmac.new(
        _key(secret), f"attendance:{token.purpose}:{token.id}".encode(), hashlib.sha256
    ).digest()[:16]
    return token.id.hex + "_" + base64.urlsafe_b64encode(signature).decode().rstrip("=")


def qr_photo(link: str) -> BufferedInputFile:
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=4)
    code.add_data(link)
    code.make(fit=True)
    canvas = code.make_image(fill_color="black", back_color="white").convert("RGB")
    with Image.open(BytesIO(files(legacy).joinpath("resources/logo.png").read_bytes())) as logo:
        logo = logo.convert("RGBA")
        size = canvas.width // 6
        logo.thumbnail((size, size), Image.Resampling.LANCZOS)
        canvas.paste(
            logo, ((canvas.width - logo.width) // 2, (canvas.height - logo.height) // 2), logo
        )
    output = BytesIO()
    canvas.save(output, format="PNG")
    return BufferedInputFile(output.getvalue(), filename="attendance-qr.png")


class AttendanceService:
    def __init__(self, session, verification_secret):
        self.session, self.secret = session, verification_secret
        self.events, self.clubs = EventService(session), ClubService(session)

    async def _identity_lock(self):
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('cu-clubs:identity-attendance',0))")
        )

    async def _event(self, public_id, *, lock=False):
        await self._identity_lock()
        event = await self.events._event(public_id, lock=lock)
        if event.status not in {"published", "held"}:
            raise ValidationError(
                "Отметка доступна только для опубликованной, не отменённой встречи"
            )
        return event

    async def _authorize(self, actor, event, *, leader=False):
        for club in await self.events._event_clubs(event):
            permission = await self.clubs.permissions(actor, club.public_id)
            if (
                (permission["is_admin"] or permission["is_leader"])
                if leader
                else permission["record_attendance"]
            ):
                return
        raise PermissionDenied("Нет права на посещаемость этой встречи")

    async def _guest_allowed(self, event):
        clubs = await self.events._event_clubs(event)
        if not any(club.level_name in {"Seed", "Pre-IPO", "IPO"} for club in clubs):
            raise PermissionDenied("Внешние гости доступны только клубам Seed и выше")

    async def _person(self, actor):
        await self._identity_lock()
        account = await self.clubs._account(actor)
        if account is None:
            raise ValidationError("Сначала открой бота")
        return account.person

    async def _token(self, actor, purpose, event=None, person=None, *, rotate=False):
        key = f"attendance-token:{purpose}:{event.id if event else person.id}"
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": key}
        )
        query = select(AttendanceQR).where(
            AttendanceQR.purpose == purpose, AttendanceQR.revoked_at.is_(None)
        )
        query = (
            query.where(AttendanceQR.event_id == event.id)
            if event
            else query.where(AttendanceQR.person_id == person.id)
        )
        tokens = list(await self.session.scalars(query.order_by(AttendanceQR.created_at.desc())))
        if tokens and not rotate:
            token = tokens[0]
        else:
            for previous in tokens:
                previous.revoked_at = now()
            token = AttendanceQR(
                id=uuid4(),
                purpose=purpose,
                event_id=event.id if event else None,
                person_id=person.id if person else None,
                created_by_telegram_id=actor,
            )
            token.token_hash = hashlib.sha256(_raw(token, self.secret).encode()).hexdigest()
            self.session.add(token)
            await self.session.flush()
        return {
            "token": _raw(token, self.secret),
            "purpose": purpose,
            "event": event.public_id if event else None,
            "person_id": str(person.id) if person else None,
        }

    async def get_personal_qr(self, actor, *, rotate=False):
        person = await self._person(actor)
        if person.kind != "guest" and (person.verified_at is None or not person.time_login):
            raise PermissionDenied("Подтверди студенческую почту, чтобы открыть личный QR")
        return await self._token(actor, "personal", person=person, rotate=rotate)

    async def event_qr(self, actor, event_public_id, *, rotate=False):
        event = await self._event(event_public_id, lock=True)
        await self._authorize(actor, event)
        return await self._token(actor, "event", event=event, rotate=rotate)

    async def _resolve(self, raw, purpose=None, *, lock=False):
        await self._identity_lock()
        try:
            token_id = UUID(hex=raw[:32])
        except (ValueError, TypeError, AttributeError):
            raise ValidationError("Некорректный QR") from None
        token = await self.session.get(
            AttendanceQR, token_id, with_for_update=lock, populate_existing=True
        )
        if (
            token is None
            or token.revoked_at is not None
            or (purpose and token.purpose != purpose)
            or not hmac.compare_digest(hashlib.sha256(raw.encode()).hexdigest(), token.token_hash)
            or not hmac.compare_digest(_raw(token, self.secret), raw)
        ):
            raise ValidationError("QR недействителен или отозван")
        return token

    async def resolve_qr(self, actor, raw):
        token = await self._resolve(raw)
        if token.purpose == "event":
            event = await self.session.get(Event, token.event_id)
            event = await self._event(event.public_id)
            await self.session.execute(
                insert(AttendanceQRGrant)
                .values(qr_id=token.id, telegram_id=actor)
                .on_conflict_do_nothing()
            )
            return {
                "purpose": "event",
                "event": event.public_id,
                "title": event.title,
                "qr_id": str(token.id),
            }
        person = await self.session.get(Person, token.person_id)
        choices = await self.recordable_events(actor)
        if not choices:
            raise PermissionDenied("Нет встреч, на которых ты можешь отмечать посетителей")
        await self.session.execute(
            insert(AttendanceQRGrant)
            .values(qr_id=token.id, telegram_id=actor)
            .on_conflict_do_nothing()
        )
        return {
            "purpose": "personal",
            "person_id": str(person.id),
            "full_name": person.full_name,
            "events": choices,
            "qr_id": str(token.id),
        }

    async def resume_raw(self, actor, qr_id):
        try:
            key = UUID(str(qr_id))
        except ValueError:
            raise ValidationError("Некорректный QR") from None
        if await self.session.get(AttendanceQRGrant, (key, actor)) is None:
            raise PermissionDenied("Сначала отсканируй QR")
        token = await self.session.get(AttendanceQR, key)
        if token is None:
            raise ValidationError("QR не найден")
        raw = _raw(token, self.secret)
        await self._resolve(raw)
        return raw

    async def revoke_qr(self, actor, raw):
        token = await self._resolve(raw)
        if token.purpose == "personal":
            if (await self._person(actor)).id != token.person_id:
                raise PermissionDenied("Нельзя отозвать чужой личный QR")
        else:
            event = await self.session.get(Event, token.event_id)
            await self._authorize(actor, event)
        token.revoked_at = now()
        self.session.add(
            Audit(
                actor_telegram_id=actor,
                action="attendance.qr_revoked",
                entity_id=token.id,
                details={"purpose": token.purpose},
            )
        )

    async def recordable_events(self, actor):
        rows = await self.session.scalars(
            select(Event)
            .where(Event.status.in_(["published", "held"]))
            .order_by(Event.starts_at.desc())
        )
        result = []
        for event in rows:
            try:
                await self._authorize(actor, event)
            except PermissionDenied:
                continue
            result.append(
                {
                    "public_id": event.public_id,
                    "title": event.title,
                    "starts_at": event.starts_at.isoformat(),
                }
            )
        return result

    async def _mark(self, actor, event, person, source, *, import_id=None, telegram_tag=None):
        previous = await self.session.get(Attendance, (event.id, person.id))
        if previous:
            return {
                "created": False,
                "event": event.public_id,
                "person_id": str(person.id),
                "full_name": person.full_name,
            }
        if person.kind == "guest":
            if source not in {"event_qr", "personal_qr"}:
                raise PermissionDenied("Гости могут отмечаться только через QR")
            await self._guest_allowed(event)
        await self.events._capture_start(event)
        registration = await self.session.get(Registration, (event.id, person.id))
        bot_account = await self.session.scalar(
            select(TelegramAccount.telegram_id).where(TelegramAccount.person_id == person.id)
        )
        snapshot = event.start_registration_snapshot
        tag = telegram_tag.lstrip("@")[:64] if isinstance(telegram_tag, str) else None
        record = Attendance(
            event_id=event.id,
            person_id=person.id,
            source=source,
            actor_telegram_id=actor,
            full_name_at_mark=person.full_name,
            time_login_at_mark=person.time_login,
            guest_at_mark=person.kind == "guest",
            was_bot_registered=bot_account is not None,
            telegram_tag_at_mark=tag,
            was_registered=registration is not None and registration.status == "active",
            registered_at_start=str(person.id) in snapshot if snapshot is not None else None,
            import_id=import_id,
        )
        self.session.add(record)
        self.session.add(
            Audit(
                actor_telegram_id=actor,
                action="attendance.marked",
                entity_id=event.id,
                details={
                    "person_id": str(person.id),
                    "source": source,
                    "import_id": str(import_id) if import_id else None,
                },
            )
        )
        await self.session.flush()
        from cu_clubs.api_service import record_change

        await record_change(self.session, event, "attendance.changed")
        return {
            "created": True,
            "event": event.public_id,
            "person_id": str(person.id),
            "full_name": person.full_name,
        }

    async def mark_event_qr(self, actor, raw):
        token = await self._resolve(raw, "event")
        model = await self.session.get(Event, token.event_id)
        event = await self._event(model.public_id, lock=True)
        await self._resolve(raw, "event", lock=True)
        person = await self._person(actor)
        if person.kind != "guest" and (
            person.kind != "student" or not person.time_login or person.verified_at is None
        ):
            raise PermissionDenied("Подтверди студенческую почту перед отметкой")
        return await self._mark(actor, event, person, "event_qr")

    async def mark_personal_qr(self, actor, event_public_id, raw):
        token = await self._resolve(raw, "personal")
        event = await self._event(event_public_id, lock=True)
        await self._authorize(actor, event)
        await self._resolve(raw, "personal", lock=True)
        person = await self.session.get(Person, token.person_id)
        if person.kind != "guest" and (person.kind != "student" or person.verified_at is None):
            raise PermissionDenied("Посетитель должен подтвердить студенческую почту")
        return await self._mark(actor, event, person, "personal_qr")

    async def register_guest_and_mark(self, actor, raw, full_name):
        token = await self._resolve(raw, "event")
        model = await self.session.get(Event, token.event_id)
        event = await self._event(model.public_id, lock=True)
        await self._resolve(raw, "event", lock=True)
        await self._guest_allowed(event)
        person = await self._person(actor)
        if person.verified_at is not None or person.time_login:
            raise ValidationError("Используй студенческий сценарий: у профиля уже есть логин")
        person.full_name, person.kind = normalize_name(full_name), "guest"
        return await self._mark(actor, event, person, "event_qr")

    async def mark_manual(self, actor, event_public_id, person_id):
        event = await self._event(event_public_id, lock=True)
        await self._authorize(actor, event)
        person = await self.session.get(Person, UUID(str(person_id)))
        if person is None or person.kind != "student" or not person.time_login:
            raise ValidationError("Для ручной отметки нужен студент с логином Тайма")
        return await self._mark(actor, event, person, "manual")

    async def _student(self, full_name, login, *, allow_name_conflict=False):
        name, login = normalize_name(full_name), normalize_login(login)
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"identity:{login}"},
        )
        person = await self.session.scalar(
            select(Person)
            .where(Person.time_login == login)
            .execution_options(populate_existing=True)
        )
        if person is not None:
            if person.full_name.casefold() != name.casefold() and not allow_name_conflict:
                raise ValidationError("ФИО не совпадает с существующим профилем. Нужна проверка")
            if person.kind == "guest":
                raise ValidationError("Нельзя импортировать гостевой профиль как студенческий")
            return person
        person = Person(full_name=name, time_login=login, kind="student", verified_at=None)
        self.session.add(person)
        await self.session.flush()
        return person

    async def mark_student(self, actor, event_public_id, full_name, login, telegram_tag=None):
        event = await self._event(event_public_id, lock=True)
        await self._authorize(actor, event)
        person = await self._student(full_name, login)
        return await self._mark(actor, event, person, "manual", telegram_tag=telegram_tag)

    async def roster(self, actor, event_public_id, *, all_participants=False):
        event = await self.events._event(event_public_id)
        await self._authorize(actor, event, leader=True)
        rows = await self.session.execute(
            select(Attendance, Person, TelegramAccount)
            .join(Person, Person.id == Attendance.person_id)
            .outerjoin(TelegramAccount, TelegramAccount.person_id == Person.id)
            .where(Attendance.event_id == event.id)
            .order_by(Person.full_name)
        )
        club_names = ", ".join(club.name for club in await self.events._event_clubs(event))
        registrations = {
            row.person_id: row
            for row in await self.session.scalars(
                select(Registration).where(Registration.event_id == event.id)
            )
        }
        result = [
            {
                "person_id": str(person.id),
                "full_name": person.full_name,
                "time_login": person.time_login,
                "telegram_tag": account.username
                if account and account.username
                else record.telegram_tag_at_mark,
                "source": record.source,
                "checked_at": record.checked_at.isoformat(),
                "was_bot_registered": record.was_bot_registered,
                "was_registered": record.was_registered,
                "registered_at_start": record.registered_at_start,
                "guest": record.guest_at_mark,
                "full_name_at_mark": record.full_name_at_mark,
                "meeting": event.title,
                "clubs": club_names,
                "date": event.starts_at.astimezone(ZoneInfo("Europe/Moscow")).isoformat(),
                "visitor_type": "Гость" if record.guest_at_mark else "Студент",
                "pre_registration": person.id in registrations or record.was_registered,
                "registration_status": {"active": "Активна", "cancelled": "Отменена"}.get(
                    registrations[person.id].status, "Нет записи"
                )
                if person.id in registrations
                else "Нет записи",
                "attendance": True,
                "marked_at": record.checked_at.astimezone(ZoneInfo("Europe/Moscow")).isoformat(),
                "bot_registered_at_mark": record.was_bot_registered,
            }
            for record, person, account in rows
        ]
        if all_participants:
            marked_ids = {row["person_id"] for row in result}
            for person_id, registration in registrations.items():
                if str(person_id) in marked_ids:
                    continue
                person = await self.session.get(Person, person_id)
                account = await self.session.scalar(
                    select(TelegramAccount).where(TelegramAccount.person_id == person_id)
                )
                result.append(
                    {
                        "person_id": str(person_id),
                        "meeting": event.title,
                        "clubs": club_names,
                        "date": event.starts_at.astimezone(ZoneInfo("Europe/Moscow")).isoformat(),
                        "full_name": person.full_name,
                        "visitor_type": "Гость" if person.kind == "guest" else "Студент",
                        "time_login": person.time_login,
                        "telegram_tag": account.username if account else None,
                        "pre_registration": True,
                        "registration_status": {"active": "Активна", "cancelled": "Отменена"}.get(
                            registration.status, registration.status
                        ),
                        "attendance": False,
                        "source": "",
                        "marked_at": "",
                        "bot_registered_at_mark": None,
                    }
                )
        return result

    async def preview_import(self, actor, event_public_id, rows, issues=None):
        event = await self._event(event_public_id)
        await self._authorize(actor, event)
        normalized, seen, errors = [], set(), list(issues or [])
        for number, row in enumerate(rows, start=2):
            row_number = row.get("row", number)
            try:
                name, login = normalize_name(row["full_name"]), normalize_login(row["time_login"])
                if login in seen:
                    continue
                seen.add(login)
                normalized.append(
                    {
                        "row": row_number,
                        "full_name": name,
                        "time_login": login,
                        "telegram_tag": row.get("telegram_tag"),
                    }
                )
            except (ValidationError, KeyError, TypeError) as error:
                errors.append(
                    {
                        "row": row_number,
                        "message": str(error)
                        if isinstance(error, ValidationError)
                        else "Некорректная строка",
                    }
                )
        batch = AttendanceImport(
            id=uuid4(),
            event_id=event.id,
            actor_telegram_id=actor,
            event_version=event.version,
            version=1,
            status="preview",
            rows=normalized,
            issues=errors,
            resolved_rows=[],
        )
        self.session.add(batch)
        await self.session.flush()
        return await self._preview(batch, event)

    async def _batch(self, actor, batch_id, expected_version=None):
        try:
            batch = await self.session.get(
                AttendanceImport, UUID(str(batch_id)), with_for_update=True
            )
        except ValueError:
            batch = None
        if batch is None or batch.actor_telegram_id != actor:
            raise PermissionDenied("Предпросмотр недоступен")
        event = await self.session.get(Event, batch.event_id)
        event = await self._event(event.public_id, lock=True)
        await self._authorize(actor, event)
        if expected_version is not None and expected_version != batch.version:
            raise ValidationError("Предпросмотр изменился. Открой актуальную версию")
        if batch.status != "confirmed" and batch.event_version != event.version:
            raise ValidationError("Встреча изменилась. Загрузи ведомость заново")
        return batch, event

    async def _preview(self, batch, event):
        fresh, existing, conflicts = 0, 0, []
        for row in batch.rows:
            person = await self.session.scalar(
                select(Person).where(Person.time_login == row["time_login"])
            )
            if (
                person is not None
                and (
                    person.kind == "guest"
                    or person.full_name.casefold() != row["full_name"].casefold()
                )
                and row["row"] not in batch.resolved_rows
            ):
                conflicts.append({**row, "existing_name": person.full_name})
                continue
            attendance = (
                await self.session.get(Attendance, (event.id, person.id)) if person else None
            )
            if attendance:
                existing += 1
            else:
                fresh += 1
        return {
            "id": str(batch.id),
            "version": batch.version,
            "event": event.public_id,
            "new": fresh,
            "existing": existing,
            "conflicts": conflicts,
            "issues": batch.issues,
            "status": batch.status,
            "result": batch.result,
        }

    async def import_preview(self, actor, batch_id):
        batch, event = await self._batch(actor, batch_id)
        return await self._preview(batch, event)

    async def resolve_import(self, actor, batch_id, rows, expected_version):
        batch, event = await self._batch(actor, batch_id, expected_version)
        if batch.status == "confirmed":
            raise ValidationError("Ведомость уже подтверждена")
        allowed = {row["row"] for row in (await self._preview(batch, event))["conflicts"]}
        if set(rows) - allowed:
            raise ValidationError("Строка не требует проверки")
        batch.resolved_rows = sorted(set(batch.resolved_rows) | set(rows))
        batch.version += 1
        return await self._preview(batch, event)

    async def confirm_import(self, actor, batch_id, expected_version):
        batch, event = await self._batch(actor, batch_id, expected_version)
        if batch.status == "confirmed":
            return batch.result
        preview = await self._preview(batch, event)
        unresolved = {row["row"] for row in preview["conflicts"]}
        new, existing = 0, 0
        for row in batch.rows:
            if row["row"] in unresolved:
                continue
            person = await self._student(
                row["full_name"],
                row["time_login"],
                allow_name_conflict=row["row"] in batch.resolved_rows,
            )
            marked = await self._mark(
                actor,
                event,
                person,
                "import",
                import_id=batch.id,
                telegram_tag=row.get("telegram_tag"),
            )
            if marked["created"]:
                new += 1
            else:
                existing += 1
        batch.status, batch.confirmed_at = "confirmed", now()
        if (
            new + existing
            and event.ends_at
            and event.ends_at <= now()
            and event.status == "published"
        ):
            event.status, event.version = "held", event.version + 1
            self.session.add(
                Audit(
                    actor_telegram_id=actor,
                    action="event.held_from_import",
                    entity_id=event.id,
                    details={"import_id": str(batch.id)},
                )
            )
        batch.result = {
            "new": new,
            "existing": existing,
            "skipped": len(unresolved),
            "issues": len(batch.issues),
            "event": event.public_id,
        }
        self.session.add(
            Audit(
                actor_telegram_id=actor,
                action="attendance.import_confirmed",
                entity_id=event.id,
                details={"import_id": str(batch.id), **batch.result},
            )
        )
        await self.session.flush()
        return batch.result

    async def mark_held(self, actor, event_public_id, expected_version):
        event = await self._event(event_public_id, lock=True)
        await self._authorize(actor, event, leader=True)
        if event.status == "held":
            return await self.events._dto(event, actor)
        self.events._version(event, expected_version)
        if event.ends_at is None or event.ends_at > now():
            raise ValidationError("Отметить встречу проведённой можно после её окончания")
        event.status, event.version = "held", event.version + 1
        self.session.add(
            Audit(
                actor_telegram_id=actor,
                action="event.held",
                entity_id=event.id,
                details={"version": event.version},
            )
        )
        return await self.events._dto(event, actor)


async def merge_attendance_people(session, old_person_id, new_person_id, actor=None):
    """Preserve the earliest mark and its historical flags when verified identities merge."""
    old_person_id, new_person_id = UUID(str(old_person_id)), UUID(str(new_person_id))
    if old_person_id == new_person_id:
        return
    records = list(
        await session.scalars(
            select(Attendance)
            .where(Attendance.person_id == old_person_id)
            .order_by(Attendance.event_id)
            .with_for_update()
        )
    )
    portable = (
        "source",
        "actor_telegram_id",
        "full_name_at_mark",
        "time_login_at_mark",
        "telegram_tag_at_mark",
        "guest_at_mark",
        "was_bot_registered",
        "was_registered",
        "registered_at_start",
        "import_id",
        "checked_at",
    )
    for old in records:
        existing = await session.get(
            Attendance, (old.event_id, new_person_id), with_for_update=True
        )
        if existing is None:
            old.person_id = new_person_id
            continue
        original = {
            "source": old.source,
            "checked_at": old.checked_at.isoformat(),
            "was_bot_registered": old.was_bot_registered,
            "guest": old.guest_at_mark,
        }
        canonical = {
            "source": existing.source,
            "checked_at": existing.checked_at.isoformat(),
            "was_bot_registered": existing.was_bot_registered,
            "guest": existing.guest_at_mark,
        }
        if old.checked_at < existing.checked_at:
            for field in portable:
                setattr(existing, field, getattr(old, field))
        session.add(
            Audit(
                actor_telegram_id=actor,
                action="attendance.identity_merged",
                entity_id=old.event_id,
                details={
                    "old_person_id": str(old_person_id),
                    "person_id": str(new_person_id),
                    "original_mark": original,
                    "canonical_mark": canonical,
                },
            )
        )
        await session.delete(old)
    tokens = await session.scalars(
        select(AttendanceQR).where(AttendanceQR.person_id == old_person_id)
    )
    for token in tokens:
        token.person_id = new_person_id
    await session.flush()
