"""Purpose-bound QR tokens and immutable first-attendance snapshots."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cu_clubs.models import Base


class AttendanceQR(Base):
    __tablename__ = "attendance_qr_tokens"
    __table_args__ = (
        CheckConstraint("purpose IN ('event','personal')", name="ck_attendance_qr_purpose"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    purpose: Mapped[str] = mapped_column(String(12))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    event_id: Mapped[UUID | None] = mapped_column(ForeignKey("events.id"), index=True)
    person_id: Mapped[UUID | None] = mapped_column(ForeignKey("people.id"), index=True)
    created_by_telegram_id: Mapped[int] = mapped_column(BigInteger)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AttendanceImport(Base):
    __tablename__ = "attendance_imports"
    __table_args__ = (
        CheckConstraint("status IN ('preview','confirmed')", name="ck_attendance_import_status"),
        CheckConstraint("version > 0", name="ck_attendance_import_version"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), index=True)
    actor_telegram_id: Mapped[int] = mapped_column(BigInteger)
    event_version: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(12), default="preview", server_default="preview")
    rows: Mapped[list[dict]] = mapped_column(JSONB)
    issues: Mapped[list[dict]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    resolved_rows: Mapped[list[int]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    result: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AttendanceQRGrant(Base):
    __tablename__ = "attendance_qr_grants"
    qr_id: Mapped[UUID] = mapped_column(ForeignKey("attendance_qr_tokens.id"), primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Attendance(Base):
    __tablename__ = "attendances"
    __table_args__ = (
        CheckConstraint(
            "source IN ('event_qr','personal_qr','manual','import')", name="ck_attendance_source"
        ),
    )
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), primary_key=True)
    person_id: Mapped[UUID] = mapped_column(ForeignKey("people.id"), primary_key=True)
    source: Mapped[str] = mapped_column(String(20))
    actor_telegram_id: Mapped[int] = mapped_column(BigInteger)
    full_name_at_mark: Mapped[str] = mapped_column(String(300))
    time_login_at_mark: Mapped[str | None] = mapped_column(String(100))
    telegram_tag_at_mark: Mapped[str | None] = mapped_column(String(64))
    category_at_mark: Mapped[str | None] = mapped_column(String(20))
    guest_at_mark: Mapped[bool] = mapped_column(Boolean)
    was_bot_registered: Mapped[bool] = mapped_column(Boolean)
    was_registered: Mapped[bool] = mapped_column(Boolean)
    registered_at_start: Mapped[bool | None] = mapped_column(Boolean)
    import_id: Mapped[UUID | None] = mapped_column(ForeignKey("attendance_imports.id"))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
