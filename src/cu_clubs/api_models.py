"""Club credentials, explicit student delegation and a durable integration feed."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
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


class ApiClient(Base):
    __tablename__ = "api_clients"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    club_id: Mapped[UUID] = mapped_column(ForeignKey("clubs.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    key_prefix: Mapped[str] = mapped_column(String(12))
    scopes: Mapped[list] = mapped_column(JSONB)
    issuer_telegram_id: Mapped[int] = mapped_column(BigInteger)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApiLink(Base):
    __tablename__ = "api_links"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    client_id: Mapped[UUID] = mapped_column(ForeignKey("api_clients.id"), index=True)
    approve_hash: Mapped[str] = mapped_column(String(64), unique=True)
    exchange_hash: Mapped[str] = mapped_column(String(64))
    telegram_id: Mapped[int] = mapped_column(BigInteger)
    person_id: Mapped[UUID | None] = mapped_column(ForeignKey("people.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exchanged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApiGrant(Base):
    __tablename__ = "api_grants"
    __table_args__ = (
        Index(
            "uq_api_active_grant",
            "client_id",
            "person_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    client_id: Mapped[UUID] = mapped_column(ForeignKey("api_clients.id"), index=True)
    person_id: Mapped[UUID] = mapped_column(ForeignKey("people.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApiIdempotency(Base):
    __tablename__ = "api_idempotency"
    client_id: Mapped[UUID] = mapped_column(ForeignKey("api_clients.id"), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(300), primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    response_encrypted: Mapped[str] = mapped_column(Text)
    status_code: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApiRateLimit(Base):
    __tablename__ = "api_rate_limits"
    client_id: Mapped[UUID] = mapped_column(ForeignKey("api_clients.id"), primary_key=True)
    bucket: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    requests: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class ApiChange(Base):
    __tablename__ = "api_changes"
    __table_args__ = (Index("ix_api_change_event", "event_id", "id"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"))
    event_public_id: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(60))
    version: Mapped[int] = mapped_column(Integer)
    clubs: Mapped[list] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ApiEndpoint(Base):
    __tablename__ = "api_webhook_endpoints"
    __table_args__ = (
        Index(
            "uq_api_active_endpoint",
            "client_id",
            "url",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    client_id: Mapped[UUID] = mapped_column(ForeignKey("api_clients.id"), index=True)
    url: Mapped[str] = mapped_column(String(2000))
    secret_encrypted: Mapped[str] = mapped_column(Text)
    event_types: Mapped[list] = mapped_column(JSONB)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApiDelivery(Base):
    __tablename__ = "api_webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("endpoint_id", "change_id", name="uq_api_delivery_change"),
        CheckConstraint(
            "status IN ('pending','processing','sent','failed','cancelled')",
            name="ck_api_delivery_status",
        ),
        Index("ix_api_delivery_due", "status", "available_at", "lease_until"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    endpoint_id: Mapped[UUID] = mapped_column(ForeignKey("api_webhook_endpoints.id"))
    change_id: Mapped[int] = mapped_column(ForeignKey("api_changes.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(80))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
