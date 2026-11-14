"""Initial club domain and durable Telegram delivery models."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Person(Base):
    __tablename__ = "people"
    __table_args__ = (
        CheckConstraint(
            "declared_category IS NULL OR "
            "declared_category IN ('student','staff','applicant','external')",
            name="ck_person_declared_category",
        ),
        CheckConstraint(
            "(declared_category IS NULL) = (category_selected_at IS NULL)",
            name="ck_person_category_selected",
        ),
    )
    declared_category: Mapped[str | None] = mapped_column(String(20))
    category_selected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    full_name: Mapped[str] = mapped_column(String(300))
    kind: Mapped[str | None] = mapped_column(String(20))
    time_login: Mapped[str | None] = mapped_column(String(100), unique=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TelegramAccount(Base):
    __tablename__ = "telegram_accounts"
    __table_args__ = (CheckConstraint("telegram_id > 0", name="ck_telegram_account_positive_id"),)
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    person_id: Mapped[UUID] = mapped_column(ForeignKey("people.id"), unique=True)
    username: Mapped[str | None] = mapped_column(String(64))
    person: Mapped[Person] = relationship(lazy="joined")


class GlobalAdmin(Base):
    __tablename__ = "global_admins"
    person_id: Mapped[UUID] = mapped_column(ForeignKey("people.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClubLevel(Base):
    __tablename__ = "club_levels"
    name: Mapped[str] = mapped_column(String(30), primary_key=True)
    sort_order: Mapped[int] = mapped_column(Integer, unique=True)


class Club(Base):
    __tablename__ = "clubs"
    __table_args__ = (CheckConstraint("version > 0", name="ck_club_positive_version"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    public_id: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    level_name: Mapped[str] = mapped_column(ForeignKey("club_levels.name"))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    direction: Mapped[str | None] = mapped_column(String(100))
    channel: Mapped[str | None] = mapped_column(String(500))
    cover: Mapped[str | None] = mapped_column(Text)
    links: Mapped[list[dict]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    public_team: Mapped[list["PublicTeam"]] = relationship(
        lazy="selectin", order_by="PublicTeam.sort_order"
    )


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        CheckConstraint("role IN ('leader','organizer')", name="ck_membership_role"),
        Index(
            "uq_membership_one_leader",
            "club_id",
            unique=True,
            postgresql_where=text("role = 'leader'"),
        ),
    )
    club_id: Mapped[UUID] = mapped_column(ForeignKey("clubs.id"), primary_key=True)
    person_id: Mapped[UUID] = mapped_column(ForeignKey("people.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(20))
    edit_profile: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    manage_events: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    record_attendance: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")


class PublicTeam(Base):
    __tablename__ = "public_team"
    __table_args__ = (UniqueConstraint("club_id", "sort_order", name="uq_public_team_position"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    club_id: Mapped[UUID] = mapped_column(ForeignKey("clubs.id"), index=True)
    sort_order: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(200))


class Subscription(Base):
    __tablename__ = "subscriptions"
    club_id: Mapped[UUID] = mapped_column(ForeignKey("clubs.id"), primary_key=True)
    person_id: Mapped[UUID] = mapped_column(ForeignKey("people.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Conversation(Base):
    __tablename__ = "conversations"
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    state: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TelegramUpdate(Base):
    __tablename__ = "telegram_updates"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','processing','done','failed')", name="ck_telegram_update_status"
        ),
        CheckConstraint("attempts >= 0", name="ck_telegram_update_attempts"),
        Index("ix_telegram_update_ready", "status", "available_at", "lease_until"),
    )
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_error: Mapped[str | None] = mapped_column(Text)


class Audit(Base):
    __tablename__ = "audit_log"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    actor_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(100))
    entity_id: Mapped[UUID | None] = mapped_column(index=True)
    details: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','processing','sent','failed','uncertain')",
            name="ck_outbox_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_outbox_attempts"),
        Index("ix_outbox_ready", "status", "available_at", "lease_until"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(100))
    recipient_telegram_id: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict] = mapped_column(JSONB)
    dedupe_key: Mapped[str] = mapped_column(String(300), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
