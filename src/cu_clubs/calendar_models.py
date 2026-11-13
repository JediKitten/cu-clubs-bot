"""Durable reconciliation state for a published meeting and its calendar cell."""

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
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


class CalendarBinding(Base):
    __tablename__ = "calendar_bindings"
    __table_args__ = (
        UniqueConstraint("event_id", "spreadsheet_id", name="uq_calendar_event_book"),
        CheckConstraint(
            "status IN ('pending','synced','conflict','error')", name="ck_calendar_status"
        ),
        CheckConstraint("revision > 0 AND attempts >= 0", name="ck_calendar_counters"),
        Index("ix_calendar_due", "next_sync_at", "status"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"))
    spreadsheet_id: Mapped[str] = mapped_column(String(200))
    sheet_id: Mapped[int | None] = mapped_column(Integer)
    sheet_title: Mapped[str | None] = mapped_column(String(100))
    row_index: Mapped[int | None] = mapped_column(Integer)
    column_index: Mapped[int | None] = mapped_column(Integer)
    baseline_text: Mapped[str | None] = mapped_column(Text)
    baseline_room: Mapped[str | None] = mapped_column(String(300))
    baseline_date: Mapped[date | None] = mapped_column(Date)
    baseline_digest: Mapped[bool | None]
    digest_weeks: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    event_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    error_code: Mapped[str | None] = mapped_column(String(100))
    conflict: Mapped[dict | None] = mapped_column(JSONB)
    resolution: Mapped[dict | None] = mapped_column(JSONB)
    pending_write: Mapped[dict | None] = mapped_column(JSONB)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_sync_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
