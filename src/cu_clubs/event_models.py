"""Persistent meetings, club collaboration and registration history."""

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
from sqlalchemy.orm import Mapped, mapped_column

from cu_clubs.models import Base


class Event(Base):
    __tablename__ = "events"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','pending_partner','published','cancelled','held')",
            name="ck_event_status",
        ),
        CheckConstraint("visibility IN ('open','closed')", name="ck_event_visibility"),
        CheckConstraint("version > 0", name="ck_event_version"),
        CheckConstraint("capacity IS NULL OR capacity > 0", name="ck_event_capacity"),
        CheckConstraint(
            "expected_attendance IS NULL OR expected_attendance > 0", name="ck_event_expected"
        ),
        CheckConstraint(
            "ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at", name="ck_event_time"
        ),
        CheckConstraint(
            "registration_deadline IS NULL OR starts_at IS NULL "
            "OR registration_deadline <= starts_at",
            name="ck_event_deadline",
        ),
        CheckConstraint("venue_type IN ('campus','external','online')", name="ck_event_venue"),
        Index("ix_event_calendar", "status", "visibility", "starts_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    public_id: Mapped[str] = mapped_column(String(32), unique=True, default=lambda: uuid4().hex)
    initiator_club_id: Mapped[UUID] = mapped_column(ForeignKey("clubs.id"))
    author_person_id: Mapped[UUID] = mapped_column(ForeignKey("people.id"))
    last_editor_telegram_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(150), default="", server_default="")
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    post_registration_text: Mapped[str] = mapped_column(Text, default="", server_default="")
    status: Mapped[str] = mapped_column(String(30), default="draft", server_default="draft")
    visibility: Mapped[str] = mapped_column(String(10), default="open", server_default="open")
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registration_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    capacity: Mapped[int | None] = mapped_column(Integer)
    expected_attendance: Mapped[int | None] = mapped_column(Integer)
    venue_type: Mapped[str] = mapped_column(String(20), default="campus", server_default="campus")
    room: Mapped[str | None] = mapped_column(String(300))
    venue_details: Mapped[str] = mapped_column(Text, default="", server_default="")
    needs_talk: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    needs_microphones: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    start_registration_snapshot: Mapped[list[str] | None] = mapped_column(JSONB)
    snapshot_starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EventClub(Base):
    __tablename__ = "event_clubs"
    __table_args__ = (
        CheckConstraint("role IN ('initiator','partner')", name="ck_event_club_role"),
    )
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), primary_key=True)
    club_id: Mapped[UUID] = mapped_column(ForeignKey("clubs.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(20))


class EventInvitation(Base):
    __tablename__ = "event_invitations"
    __table_args__ = (
        UniqueConstraint("event_id", "version", name="uq_event_invitation_version"),
        CheckConstraint(
            "status IN ('pending','accepted','declined','superseded')",
            name="ck_event_invitation_status",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), index=True)
    partner_club_id: Mapped[UUID] = mapped_column(ForeignKey("clubs.id"))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    decided_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Registration(Base):
    __tablename__ = "registrations"
    __table_args__ = (
        CheckConstraint("status IN ('active','cancelled')", name="ck_registration_status"),
        Index("ix_registration_person", "person_id", "status"),
    )
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), primary_key=True)
    person_id: Mapped[UUID] = mapped_column(ForeignKey("people.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active")
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(20), default="telegram", server_default="telegram")


class RegistrationHistory(Base):
    __tablename__ = "registration_history"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), index=True)
    person_id: Mapped[UUID] = mapped_column(ForeignKey("people.id"))
    status: Mapped[str] = mapped_column(String(20))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EventTemplate(Base):
    __tablename__ = "event_templates"
    __table_args__ = (UniqueConstraint("club_id", "name", name="uq_event_template_name"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    public_id: Mapped[str] = mapped_column(String(32), unique=True, default=lambda: uuid4().hex)
    club_id: Mapped[UUID] = mapped_column(ForeignKey("clubs.id"))
    name: Mapped[str] = mapped_column(String(150))
    fields: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
