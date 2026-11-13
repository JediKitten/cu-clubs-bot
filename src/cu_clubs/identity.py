"""Identity and administrator commands. The caller owns the transaction.

Invalid verification attempts return a result instead of raising: the caller must
commit these results so that the attempt counter cannot be reset by a rollback.
"""

import hashlib
import hmac
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from cu_clubs.identity_models import EmailChallenge
from cu_clubs.models import Audit, Club, ClubLevel, Membership, Outbox, Person, TelegramAccount
from cu_clubs.services import ClubService, PermissionDenied, ValidationError, _telegram_id

EMAIL_DOMAIN = "edu.centraluniversity.ru"
CODE_TTL = timedelta(minutes=10)
RESEND_DELAY = timedelta(seconds=60)
MAX_ATTEMPTS = 5
MAX_HOURLY_REQUESTS = 5


def normalize_login(value: str) -> str:
    value = value.strip().lower()
    if "@" in value:
        login, _, domain = value.partition("@")
        if domain != EMAIL_DOMAIN:
            raise ValidationError(f"Нужна университетская почта @{EMAIL_DOMAIN}")
        value = login
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,98}[a-z0-9]|[a-z0-9]", value):
        raise ValidationError("Введи логин Тайма без пробелов или университетскую почту")
    if ".." in value:
        raise ValidationError("Некорректный логин Тайма")
    return value


def normalize_name(value: str) -> str:
    value = " ".join(value.split())
    if not 3 <= len(value) <= 300 or len(value.split()) < 2:
        raise ValidationError("Введи имя и фамилию полностью")
    if any(ord(char) < 32 for char in value):
        raise ValidationError("Некорректное ФИО")
    return value


def _now(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        raise ValueError("An aware timestamp is required")
    return value.astimezone(UTC)


def _secret(value: str) -> bytes:
    if len(value) < 32:
        raise ValidationError("Подтверждение почты временно недоступно")
    return value.encode()


def _code(challenge: EmailChallenge, secret: str) -> str:
    message = f"code:{challenge.id}:{challenge.telegram_id}:{challenge.time_login}".encode()
    digest = hmac.new(_secret(secret), message, hashlib.sha256).digest()
    return f"{int.from_bytes(digest[:8], 'big') % 1_000_000:06d}"


def _digest(challenge_id: UUID, code: str, secret: str) -> str:
    return hmac.new(
        _secret(secret), f"digest:{challenge_id}:{code}".encode(), hashlib.sha256
    ).hexdigest()


def _profile(person: Person, telegram_id: int) -> dict:
    return {
        "id": str(person.id),
        "telegram_id": telegram_id,
        "full_name": person.full_name,
        "time_login": person.time_login,
        "kind": person.kind,
        "verified_at": person.verified_at,
    }


class IdentityService:
    def __init__(
        self,
        session: AsyncSession,
        verification_secret: str,
        *,
        mail_available: bool,
        now: datetime | None = None,
    ):
        self.session = session
        self.secret = verification_secret
        self.mail_available = mail_available
        self.now = _now(now)

    async def profile(self, telegram_id: int) -> dict:
        account = await ClubService(self.session)._account(telegram_id)
        if account is None:
            raise ValidationError("Сначала открой бота")
        return _profile(account.person, telegram_id)

    async def _lock_person(self, telegram_id: int) -> Person:
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('cu-clubs:identity-attendance',0))")
        )
        telegram_id = _telegram_id(telegram_id)
        await self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": telegram_id})
        person = await self.session.scalar(
            select(Person)
            .join(TelegramAccount, TelegramAccount.person_id == Person.id)
            .where(TelegramAccount.telegram_id == telegram_id)
            .with_for_update(of=Person)
            .execution_options(populate_existing=True)
        )
        if person is None:
            raise ValidationError("Сначала открой бота")
        return person

    async def start_verification(self, telegram_id: int, full_name: str, login: str) -> dict:
        if not self.mail_available:
            raise ValidationError(
                "Подтверждение почты пока недоступно: отправка писем ещё не настроена"
            )
        _secret(self.secret)
        login, full_name = normalize_login(login), normalize_name(full_name)
        person = await self._lock_person(telegram_id)
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"identity:{login}"},
        )
        from cu_clubs.identity_merge import imported_person

        _existing, occupied = await imported_person(self.session, login, person.id)
        if occupied:
            raise ValidationError("Эта почта уже привязана к другому профилю")
        if person.verified_at is not None and person.time_login == login:
            raise ValidationError("Эта почта уже подтверждена")
        latest = await self.session.scalar(
            select(EmailChallenge)
            .where(
                or_(EmailChallenge.telegram_id == telegram_id, EmailChallenge.time_login == login)
            )
            .order_by(EmailChallenge.created_at.desc())
            .limit(1)
        )
        if latest and latest.created_at + RESEND_DELAY > self.now:
            raise ValidationError("Повторный код можно запросить через минуту")
        for predicate in (
            EmailChallenge.telegram_id == telegram_id,
            EmailChallenge.time_login == login,
        ):
            count = await self.session.scalar(
                select(func.count())
                .select_from(EmailChallenge)
                .where(predicate, EmailChallenge.created_at > self.now - timedelta(hours=1))
            )
            if count >= MAX_HOURLY_REQUESTS:
                raise ValidationError("Слишком много запросов. Попробуй через час")
        await self.session.execute(
            update(EmailChallenge)
            .where(
                EmailChallenge.telegram_id == telegram_id,
                EmailChallenge.consumed_at.is_(None),
                EmailChallenge.invalidated_at.is_(None),
            )
            .values(invalidated_at=self.now)
        )
        challenge = EmailChallenge(
            id=uuid4(),
            person_id=person.id,
            telegram_id=telegram_id,
            time_login=login,
            full_name=full_name,
            created_at=self.now,
            expires_at=self.now + CODE_TTL,
            attempts=0,
        )
        challenge.code_digest = _digest(challenge.id, _code(challenge, self.secret), self.secret)
        self.session.add(challenge)
        self.session.add(
            Outbox(
                kind="email.verification",
                recipient_telegram_id=telegram_id,
                payload={"challenge_id": str(challenge.id)},
                dedupe_key=f"email.verification:{challenge.id}",
            )
        )
        await self.session.flush()
        return {
            "challenge_id": str(challenge.id),
            "email": f"{login}@{EMAIL_DOMAIN}",
            "expires_at": challenge.expires_at,
            "resend_at": self.now + RESEND_DELAY,
        }

    async def verify(self, telegram_id: int, code: str) -> dict:
        person = await self._lock_person(telegram_id)
        challenge = await self.session.scalar(
            select(EmailChallenge)
            .where(
                EmailChallenge.telegram_id == telegram_id, EmailChallenge.invalidated_at.is_(None)
            )
            .order_by(EmailChallenge.created_at.desc())
            .with_for_update()
            .limit(1)
        )
        if challenge is None or challenge.consumed_at is not None:
            return {"status": "missing"}
        if challenge.expires_at <= self.now:
            return {"status": "expired"}
        if challenge.attempts >= MAX_ATTEMPTS:
            return {"status": "exhausted", "remaining_attempts": 0}
        code = code.strip()
        challenge.attempts += 1
        candidate = _digest(challenge.id, code, self.secret)
        if not re.fullmatch(r"[0-9]{6}", code) or not hmac.compare_digest(
            candidate, challenge.code_digest
        ):
            await self.session.flush()
            remaining = MAX_ATTEMPTS - challenge.attempts
            return {
                "status": "invalid" if remaining else "exhausted",
                "remaining_attempts": remaining,
            }
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"identity:{challenge.time_login}"},
        )
        from cu_clubs.identity_merge import imported_person, merge_into_imported

        existing, occupied = await imported_person(self.session, challenge.time_login, person.id)
        if occupied:
            challenge.invalidated_at = self.now
            return {"status": "occupied"}
        if existing is not None:
            person = await merge_into_imported(self.session, person, existing, telegram_id)
        person.full_name, person.time_login = challenge.full_name, challenge.time_login
        person.kind, person.verified_at = "student", self.now
        challenge.consumed_at = self.now
        self.session.add(
            Audit(
                actor_telegram_id=telegram_id,
                action="identity.email.verified",
                entity_id=person.id,
                details={"challenge_id": str(challenge.id)},
            )
        )
        await self.session.flush()
        return {"status": "verified", "profile": _profile(person, telegram_id)}


async def verification_delivery(
    session: AsyncSession, challenge_id: str, secret: str, *, now: datetime | None = None
) -> dict | None:
    """Resolve a still-current challenge for SMTP, without persisting the code."""
    challenge = await session.get(EmailChallenge, UUID(challenge_id))
    if (
        challenge is None
        or challenge.consumed_at is not None
        or challenge.invalidated_at is not None
        or challenge.attempts >= MAX_ATTEMPTS
        or challenge.expires_at <= _now(now)
    ):
        return None
    code = _code(challenge, secret)
    if not hmac.compare_digest(challenge.code_digest, _digest(challenge.id, code, secret)):
        return None
    return {"email": f"{challenge.time_login}@{EMAIL_DOMAIN}", "code": code}


class AdminService:
    def __init__(self, session: AsyncSession, *, allow_unverified_leaders: bool = False):
        self.session = session
        self.clubs = ClubService(session)
        self.allow_unverified_leaders = allow_unverified_leaders

    async def _authorize(self, actor: int) -> None:
        if not await self.clubs.is_admin(actor):
            raise PermissionDenied(
                "Создание клуба и назначение лидера доступны только администрации"
            )

    async def _leader(self, telegram_id: int) -> TelegramAccount:
        account = await self.clubs._account(telegram_id)
        if account is None or (
            not self.allow_unverified_leaders
            and (account.person.verified_at is None or account.person.kind != "student")
        ):
            raise ValidationError("Лидер должен сначала подтвердить университетскую почту в боте")
        return account

    async def _level(self, level: str) -> None:
        if await self.session.get(ClubLevel, level) is None:
            raise ValidationError("Неизвестный уровень клуба")

    async def create_club(self, actor: int, name: str, level: str, leader_telegram_id: int) -> dict:
        await self._authorize(actor)
        name = " ".join(name.split())
        if not 2 <= len(name) <= 100:
            raise ValidationError("Название должно содержать от 2 до 100 символов")
        await self._level(level)
        leader = await self._leader(leader_telegram_id)
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"club-name:{name.casefold()}"},
        )
        if await self.session.scalar(select(Club.id).where(func.lower(Club.name) == name.lower())):
            raise ValidationError("Клуб с таким названием уже существует")
        club = Club(public_id=f"club_{uuid4().hex[:16]}", name=name, level_name=level)
        self.session.add(club)
        await self.session.flush()
        self.session.add(Membership(club_id=club.id, person_id=leader.person_id, role="leader"))
        self.session.add(
            Audit(
                actor_telegram_id=actor,
                action="club.created",
                entity_id=club.id,
                details={
                    "public_id": club.public_id,
                    "level": level,
                    "leader_telegram_id": leader_telegram_id,
                },
            )
        )
        self.session.add(
            Outbox(
                kind="club.created",
                recipient_telegram_id=leader_telegram_id,
                payload={
                    "club_id": str(club.id),
                    "public_id": club.public_id,
                    "name": club.name,
                    "level": level,
                },
                dedupe_key=f"club.created:{club.id}:{leader_telegram_id}",
            )
        )
        await self.session.flush()
        return self.clubs._dto(await self.clubs._club(club.public_id))

    async def set_level(
        self, actor: int, public_id: str, level: str, *, expected_version: int | None = None
    ) -> dict:
        await self._authorize(actor)
        await self._level(level)
        club = await self.clubs._club(public_id, lock=True)
        self._version(club, expected_version)
        if club.level_name != level:
            previous, club.level_name = club.level_name, level
            club.version += 1
            self.session.add(
                Audit(
                    actor_telegram_id=actor,
                    action="club.level.changed",
                    entity_id=club.id,
                    details={"previous": previous, "level": level},
                )
            )
            recipient = await self.session.scalar(
                select(TelegramAccount.telegram_id)
                .join(Membership, Membership.person_id == TelegramAccount.person_id)
                .where(Membership.club_id == club.id, Membership.role == "leader")
            )
            if recipient is not None:
                self.session.add(
                    Outbox(
                        kind="club.level.changed",
                        recipient_telegram_id=recipient,
                        payload={
                            "public_id": club.public_id,
                            "name": club.name,
                            "level": level,
                            "previous": previous,
                        },
                        dedupe_key=f"club.level.changed:{club.id}:{club.version}",
                    )
                )
        await self.session.flush()
        return self.clubs._dto(club)

    async def set_leader(
        self, actor: int, public_id: str, new_id: int, *, expected_version: int | None = None
    ) -> dict:
        await self._authorize(actor)
        new = await self._leader(new_id)
        club = await self.clubs._club(public_id, lock=True)
        self._version(club, expected_version)
        old = await self.session.scalar(
            select(Membership)
            .where(Membership.club_id == club.id, Membership.role == "leader")
            .with_for_update()
        )
        if old is not None and old.person_id == new.person_id:
            return self.clubs._dto(club)
        if old is not None:
            old.role = "organizer"
            await self.session.flush()
        membership = await self.clubs._membership(club.id, new.person_id)
        if membership is None:
            membership = Membership(club_id=club.id, person_id=new.person_id, role="leader")
            self.session.add(membership)
        else:
            membership.role = "leader"
        club.version += 1
        self.session.add(
            Audit(
                actor_telegram_id=actor,
                action="club.leader.changed",
                entity_id=club.id,
                details={
                    "previous_person_id": str(old.person_id) if old is not None else None,
                    "leader_telegram_id": new_id,
                },
            )
        )
        self.session.add(
            Outbox(
                kind="club.leader.assigned",
                recipient_telegram_id=new_id,
                payload={"public_id": club.public_id, "name": club.name, "level": club.level_name},
                dedupe_key=f"club.leader.assigned:{club.id}:{club.version}",
            )
        )
        await self.session.flush()
        return self.clubs._dto(club)

    @staticmethod
    def _version(club: Club, expected_version: int | None) -> None:
        if expected_version is not None and club.version != expected_version:
            raise ValidationError("Карточка изменилась. Открой актуальную версию")
