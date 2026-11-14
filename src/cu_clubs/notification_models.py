"""Confirmed event broadcasts; outbox payloads hold references, never message bodies."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from cu_clubs.models import Base


class Broadcast(Base):
    __tablename__ = "notification_broadcasts"
    __table_args__ = (
        CheckConstraint("audience IN ('registered','attended')", name="ck_broadcast_audience"),
        CheckConstraint("length(body) BETWEEN 1 AND 500", name="ck_broadcast_body"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), index=True)
    actor_telegram_id: Mapped[int] = mapped_column(BigInteger)
    audience: Mapped[str] = mapped_column(String(20))
    body: Mapped[str] = mapped_column(Text)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
