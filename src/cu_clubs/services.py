"""Shared club operations. Callers own the transaction and delivery happens later."""

from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import delete, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cu_clubs.models import (
    Audit,
    Club,
    ClubLevel,
    GlobalAdmin,
    Membership,
    Outbox,
    Person,
    PublicTeam,
    Subscription,
    TelegramAccount,
)


class PermissionDenied(ValueError):
    pass


class ClubNotFound(ValueError):
    pass


class ValidationError(ValueError):
    pass


def _telegram_id(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2**63:
        raise ValidationError("Нужен положительный числовой Telegram ID пользователя")
    return value


class ClubService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _account(self, telegram_id: int) -> TelegramAccount | None:
        return await self.session.scalar(
            select(TelegramAccount)
            .where(TelegramAccount.telegram_id == _telegram_id(telegram_id))
            .execution_options(populate_existing=True)
        )

    async def _membership(self, club_id: UUID, person_id: UUID) -> Membership | None:
        return await self.session.scalar(
            select(Membership)
            .where(Membership.club_id == club_id, Membership.person_id == person_id)
            .execution_options(populate_existing=True)
        )

    async def get_or_create_person(
        self, telegram_id: int, full_name: str, username: str | None = None
    ) -> dict:
        telegram_id = _telegram_id(telegram_id)
        await self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": telegram_id})
        account = await self._account(telegram_id)
        full_name = full_name.strip()[:300] or f"Telegram {telegram_id}"
        username = username.lstrip("@")[:64] if username else None
        if account is None:
            person = Person(full_name=full_name)
            self.session.add(person)
            await self.session.flush()
            account = TelegramAccount(
                telegram_id=telegram_id, person_id=person.id, username=username
            )
            self.session.add(account)
            await self.session.flush()
        else:
            person = account.person
            person.full_name = full_name
            account.username = username
        return {
            "id": str(person.id),
            "telegram_id": telegram_id,
            "full_name": person.full_name,
            "username": account.username,
        }

    async def _club(self, public_id: str, *, lock: bool = False) -> Club:
        query = (
            select(Club)
            .where(Club.public_id == public_id)
            .options(selectinload(Club.public_team))
            .execution_options(populate_existing=True)
        )
        if lock:
            query = query.with_for_update()
        club = await self.session.scalar(query)
        if club is None:
            raise ClubNotFound("Клуб не найден")
        return club

    @staticmethod
    def _dto(club: Club) -> dict:
        return {
            "id": str(club.id),
            "public_id": club.public_id,
            "name": club.name,
            "level": club.level_name,
            "description": club.description,
            "channel": club.channel,
            "cover": club.cover,
            "links": club.links,
            "public_team": [
                {"name": member.name, "role": member.role} for member in club.public_team
            ],
            "version": club.version,
        }

    async def list_clubs(self) -> list[dict]:
        clubs = await self.session.scalars(
            select(Club).options(selectinload(Club.public_team)).order_by(Club.name)
        )
        return [self._dto(club) for club in clubs]

    async def get_club(self, public_id: str) -> dict | None:
        try:
            return self._dto(await self._club(public_id))
        except ClubNotFound:
            return None

    async def is_admin(self, telegram_id: int) -> bool:
        account = await self._account(telegram_id)
        return (
            account is not None
            and await self.session.scalar(
                select(GlobalAdmin.person_id).where(GlobalAdmin.person_id == account.person_id)
            )
            is not None
        )

    async def permissions(self, telegram_id: int, public_id: str) -> dict[str, bool]:
        club = await self._club(public_id)
        account = await self._account(telegram_id)
        admin = await self.is_admin(telegram_id)
        membership = await self._membership(club.id, account.person_id) if account else None
        leader = membership is not None and membership.role == "leader"
        return {
            "is_admin": admin,
            "is_leader": leader,
            "edit_profile": admin or leader or bool(membership and membership.edit_profile),
            "manage_events": admin or leader or bool(membership and membership.manage_events),
            "record_attendance": admin
            or leader
            or bool(membership and membership.record_attendance),
        }

    async def authorize(self, telegram_id: int, public_id: str, permission: str) -> None:
        permissions = await self.permissions(telegram_id, public_id)
        if permission not in permissions or not permissions[permission]:
            raise PermissionDenied("Нет права на это действие")

    async def managed_clubs(self, telegram_id: int) -> list[dict]:
        if await self.is_admin(telegram_id):
            return await self.list_clubs()
        account = await self._account(telegram_id)
        if account is None:
            return []
        clubs = await self.session.scalars(
            select(Club)
            .join(Membership)
            .where(
                Membership.person_id == account.person_id,
                or_(
                    Membership.role == "leader",
                    Membership.edit_profile,
                    Membership.manage_events,
                    Membership.record_attendance,
                ),
            )
            .options(selectinload(Club.public_team))
            .order_by(Club.name)
        )
        return [self._dto(club) for club in clubs]

    async def update_club(
        self,
        telegram_id: int,
        public_id: str,
        field: str,
        value: Any,
        *,
        expected_version: int | None = None,
    ) -> dict:
        club = await self._club(public_id, lock=True)
        await self.authorize(telegram_id, public_id, "edit_profile")
        if expected_version is not None and club.version != expected_version:
            raise ValidationError("Карточка изменилась. Открой актуальную версию")
        if field == "links":
            if not isinstance(value, list) or len(value) > 20:
                raise ValidationError("Ссылки должны быть списком до 20 названий и адресов")
            normalized_links = []
            for item in value:
                if not isinstance(item, dict):
                    raise ValidationError("Укажи название и адрес ссылки")
                label, url = item.get("label"), item.get("url")
                if not isinstance(label, str) or not 1 <= len(label.strip()) <= 100:
                    raise ValidationError("Название ссылки: от 1 до 100 символов")
                if not isinstance(url, str) or len(url.strip()) > 2000:
                    raise ValidationError("Адрес ссылки: до 2000 символов")
                self._validate_url(url.strip())
                normalized_links.append({"label": label.strip(), "url": url.strip()})
            club.links = normalized_links
        elif field == "public_team":
            if not isinstance(value, list) or len(value) > 100:
                raise ValidationError("Команда должна быть списком до 100 имён и ролей")
            normalized = []
            for item in value:
                if not isinstance(item, dict):
                    raise ValidationError("Укажи имя и роль участника команды")
                name, role = item.get("name"), item.get("role", "")
                if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
                    raise ValidationError("Имя участника: от 1 до 200 символов")
                if not isinstance(role, str) or len(role.strip()) > 200:
                    raise ValidationError("Роль участника: до 200 символов")
                normalized.append({"name": name.strip(), "role": role.strip()})
            await self.session.execute(delete(PublicTeam).where(PublicTeam.club_id == club.id))
            self.session.add_all(
                PublicTeam(club_id=club.id, sort_order=i, **item)
                for i, item in enumerate(normalized)
            )
            await self.session.flush()
            await self.session.refresh(club, attribute_names=["public_team"])
        elif field in {"name", "description", "channel", "cover"}:
            if value is None and field in {"channel", "cover"}:
                normalized = None
            elif isinstance(value, str):
                normalized = value.strip()
                limits = {"name": 200, "description": 10000, "channel": 500, "cover": 2000}
                if len(normalized) > limits[field] or (field == "name" and not normalized):
                    raise ValidationError(f"Недопустимая длина поля {field}")
                if field in {"channel", "cover"} and not normalized:
                    normalized = None
                if field == "channel" and normalized:
                    self._validate_url(normalized)
            else:
                raise ValidationError("Нужно текстовое значение")
            setattr(club, field, normalized)
        else:
            raise ValidationError("Это поле нельзя изменить через профиль клуба")
        club.version += 1
        self.session.add(
            Audit(
                actor_telegram_id=telegram_id,
                action="club.profile.updated",
                entity_id=club.id,
                details={"field": field, "version": club.version},
            )
        )
        await self.session.flush()
        return self._dto(club)

    @staticmethod
    def _validate_url(value: str) -> None:
        try:
            url = urlsplit(value)
            valid = url.scheme in {"http", "https"} and url.hostname and not url.username
        except ValueError:
            valid = False
        if not valid or any(character.isspace() for character in value):
            raise ValidationError("Нужна полная ссылка https:// на канал или сайт")

    async def get_subscription(self, telegram_id: int, public_id: str) -> bool:
        club = await self._club(public_id)
        account = await self._account(telegram_id)
        return (
            account is not None
            and await self.session.get(Subscription, (club.id, account.person_id)) is not None
        )

    async def set_subscription(self, telegram_id: int, public_id: str, subscribed: bool) -> bool:
        if not isinstance(subscribed, bool):
            raise ValidationError("Подписка должна быть переключателем да/нет")
        club = await self._club(public_id)
        account = await self._account(telegram_id)
        if account is None:
            raise ValidationError("Сначала открой бота")
        if subscribed:
            await self.session.execute(
                insert(Subscription)
                .values(club_id=club.id, person_id=account.person_id)
                .on_conflict_do_nothing()
            )
        else:
            await self.session.execute(
                delete(Subscription).where(
                    Subscription.club_id == club.id, Subscription.person_id == account.person_id
                )
            )
        return subscribed

    async def _authorize_team(self, telegram_id: int, public_id: str) -> None:
        permissions = await self.permissions(telegram_id, public_id)
        if not permissions["is_admin"] and not permissions["is_leader"]:
            raise PermissionDenied("Изменять организаторов может лидер или администратор")

    async def list_organizers(self, telegram_id: int, public_id: str) -> list[dict]:
        await self._authorize_team(telegram_id, public_id)
        club = await self._club(public_id)
        rows = await self.session.execute(
            select(Membership, TelegramAccount, Person)
            .join(Person, Person.id == Membership.person_id)
            .join(TelegramAccount, TelegramAccount.person_id == Person.id)
            .where(Membership.club_id == club.id)
            .order_by(Membership.role, Person.full_name)
        )
        return [
            {
                "person_id": str(person.id),
                "telegram_id": account.telegram_id,
                "full_name": person.full_name,
                "username": account.username,
                "role": member.role,
                "edit_profile": member.edit_profile,
                "manage_events": member.manage_events,
                "record_attendance": member.record_attendance,
            }
            for member, account, person in rows
        ]

    async def organizer_names(self, telegram_id: int, public_id: str) -> list[dict]:
        if not any((await self.permissions(telegram_id, public_id)).values()):
            raise PermissionDenied("Нет доступа к управлению этим клубом")
        club = await self._club(public_id)
        rows = await self.session.execute(
            select(Person.full_name, TelegramAccount.username)
            .join(TelegramAccount, TelegramAccount.person_id == Person.id)
            .join(Membership, Membership.person_id == Person.id)
            .where(Membership.club_id == club.id)
            .order_by(Membership.role, Person.full_name)
        )
        return [{"full_name": name, "username": username} for name, username in rows]

    async def set_organizer(
        self,
        telegram_id: int,
        public_id: str,
        target_telegram_id: int,
        *,
        edit_profile: bool = True,
        manage_events: bool = True,
        record_attendance: bool = True,
    ) -> None:
        club = await self._club(public_id, lock=True)
        await self._authorize_team(telegram_id, public_id)
        target = await self._account(target_telegram_id)
        if target is None:
            raise ValidationError("Пользователь должен сначала открыть бота")
        membership = await self._membership(club.id, target.person_id)
        if membership and membership.role == "leader":
            raise ValidationError("Права лидера меняются отдельным назначением")
        values = dict(
            edit_profile=edit_profile,
            manage_events=manage_events,
            record_attendance=record_attendance,
        )
        if not all(isinstance(value, bool) for value in values.values()):
            raise ValidationError("Разрешения должны быть переключателями да/нет")
        if membership is None:
            self.session.add(
                Membership(club_id=club.id, person_id=target.person_id, role="organizer", **values)
            )
        else:
            for key, value in values.items():
                setattr(membership, key, value)
        self.session.add(
            Audit(
                actor_telegram_id=telegram_id,
                action="club.organizer.updated",
                entity_id=club.id,
                details={"target_telegram_id": target_telegram_id, **values},
            )
        )
        await self.session.flush()

    async def remove_organizer(
        self, telegram_id: int, public_id: str, target_telegram_id: int
    ) -> None:
        club = await self._club(public_id, lock=True)
        await self._authorize_team(telegram_id, public_id)
        target = await self._account(target_telegram_id)
        if target is None:
            return
        membership = await self._membership(club.id, target.person_id)
        if membership and membership.role == "leader":
            raise ValidationError("Нельзя удалить лидера через список организаторов")
        if membership:
            await self.session.delete(membership)
            self.session.add(
                Audit(
                    actor_telegram_id=telegram_id,
                    action="club.organizer.removed",
                    entity_id=club.id,
                    details={"target_telegram_id": target_telegram_id},
                )
            )
            await self.session.flush()

    async def bootstrap(self, admin_ids: list[int], leader_id: int) -> dict:
        leader_id = _telegram_id(leader_id)
        ids = sorted({_telegram_id(value) for value in admin_ids} | {leader_id})
        await self.session.execute(text("SELECT pg_advisory_xact_lock(-7311262026)"))
        for rank, level in enumerate(("Concept", "Pre-Seed", "Seed", "Pre-IPO", "IPO")):
            await self.session.execute(
                insert(ClubLevel).values(name=level, sort_order=rank).on_conflict_do_nothing()
            )
        for telegram_id in ids:
            account = await self._account(telegram_id)
            if account is None:
                await self.get_or_create_person(telegram_id, f"Telegram {telegram_id}")
                account = await self._account(telegram_id)
            if telegram_id in admin_ids:
                granted = await self.session.scalar(
                    insert(GlobalAdmin)
                    .values(person_id=account.person_id)
                    .on_conflict_do_nothing()
                    .returning(GlobalAdmin.person_id)
                )
                if granted is not None:
                    self.session.add(
                        Audit(
                            action="admin.bootstrap.granted",
                            entity_id=account.person_id,
                            details={"telegram_id": telegram_id},
                        )
                    )
        club = await self.get_club("cinema")
        if club is not None:
            return club
        model = Club(public_id="cinema", name="Киноклуб", level_name="Seed")
        self.session.add(model)
        await self.session.flush()
        leader = await self._account(leader_id)
        self.session.add(Membership(club_id=model.id, person_id=leader.person_id, role="leader"))
        self.session.add(
            Audit(
                actor_telegram_id=leader_id,
                action="club.created",
                entity_id=model.id,
                details={"public_id": "cinema", "level": "Seed"},
            )
        )
        self.session.add(
            Outbox(
                kind="club.created",
                recipient_telegram_id=leader_id,
                payload={"club_id": str(model.id), "public_id": "cinema", "name": "Киноклуб"},
                dedupe_key=f"club.created:cinema:{leader_id}",
            )
        )
        await self.session.flush()
        return self._dto(await self._club("cinema"))
