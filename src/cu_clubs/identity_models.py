"""Durable, single-use email challenges; no email code is stored in plaintext."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from cu_clubs.models import Base


class EmailChallenge(Base):
    __tablename__ = "email_challenges"
    __table_args__ = (
        CheckConstraint("attempts >= 0 AND attempts <= 5", name="ck_email_challenge_attempts"),
        Index("ix_email_challenge_account_created", "telegram_id", "created_at"),
        Index("ix_email_challenge_login_created", "time_login", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    person_id: Mapped[UUID] = mapped_column(ForeignKey("people.id"))
    telegram_id: Mapped[int] = mapped_column(BigInteger)
    time_login: Mapped[str] = mapped_column(String(100))
    full_name: Mapped[str] = mapped_column(String(300))
    code_digest: Mapped[str] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
