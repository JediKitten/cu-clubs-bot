"""Club integration credentials, delegated grants and durable changes/webhooks.

Revision ID: 20261114_0009
Revises: 20261114_0008
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "20261114_0009"
down_revision = "20261114_0008"
branch_labels = None
depends_on = None


def uid(name="id", *, target=None, primary=False, nullable=False):
    args = [sa.ForeignKey(target)] if target else []
    return sa.Column(name, sa.Uuid(), *args, primary_key=primary, nullable=nullable)


def stamp(name, *, nullable=True, default=False):
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.func.now() if default else None,
    )


def upgrade():
    op.create_table(
        "api_clients",
        uid(primary=True),
        uid("club_id", target="clubs.id"),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("key_prefix", sa.String(12), nullable=False),
        sa.Column("scopes", pg.JSONB(), nullable=False),
        sa.Column("issuer_telegram_id", sa.BigInteger(), nullable=False),
        stamp("revoked_at"),
        stamp("created_at", nullable=False, default=True),
    )
    op.create_table(
        "api_links",
        uid(primary=True),
        uid("client_id", target="api_clients.id"),
        sa.Column("approve_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("exchange_hash", sa.String(64), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        uid("person_id", target="people.id", nullable=True),
        stamp("approved_at"),
        stamp("exchanged_at"),
        stamp("expires_at", nullable=False),
        stamp("created_at", nullable=False, default=True),
    )
    op.create_table(
        "api_grants",
        uid(primary=True),
        uid("client_id", target="api_clients.id"),
        uid("person_id", target="people.id"),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        stamp("expires_at", nullable=False),
        stamp("revoked_at"),
        stamp("created_at", nullable=False, default=True),
    )
    op.create_index(
        "uq_api_active_grant",
        "api_grants",
        ["client_id", "person_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_table(
        "api_idempotency",
        uid("client_id", target="api_clients.id", primary=True),
        sa.Column("namespace", sa.String(300), primary_key=True),
        sa.Column("key_hash", sa.String(64), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_encrypted", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        stamp("created_at", nullable=False, default=True),
    )
    op.create_table(
        "api_rate_limits",
        uid("client_id", target="api_clients.id", primary=True),
        sa.Column("bucket", sa.BigInteger(), primary_key=True),
        sa.Column("requests", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_table(
        "api_changes",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        uid("event_id", target="events.id"),
        sa.Column("event_public_id", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(60), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("clubs", pg.JSONB(), nullable=False),
        stamp("occurred_at", nullable=False, default=True),
    )
    op.create_index("ix_api_change_event", "api_changes", ["event_id", "id"])
    op.create_table(
        "api_webhook_endpoints",
        uid(primary=True),
        uid("client_id", target="api_clients.id"),
        sa.Column("url", sa.String(2000), nullable=False),
        sa.Column("secret_encrypted", sa.Text(), nullable=False),
        sa.Column("event_types", pg.JSONB(), nullable=False),
        stamp("revoked_at"),
        stamp("created_at", nullable=False, default=True),
    )
    op.create_index(
        "uq_api_active_endpoint",
        "api_webhook_endpoints",
        ["client_id", "url"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_table(
        "api_webhook_deliveries",
        uid(primary=True),
        uid("endpoint_id", target="api_webhook_endpoints.id"),
        sa.Column("change_id", sa.BigInteger(), sa.ForeignKey("api_changes.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        stamp("available_at", nullable=False, default=True),
        stamp("lease_until"),
        sa.Column("last_error", sa.String(80)),
        stamp("delivered_at"),
        sa.UniqueConstraint("endpoint_id", "change_id", name="uq_api_delivery_change"),
        sa.CheckConstraint(
            "status IN ('pending','processing','sent','failed','cancelled')",
            name="ck_api_delivery_status",
        ),
    )
    op.create_index(
        "ix_api_delivery_due", "api_webhook_deliveries", ["status", "available_at", "lease_until"]
    )
    for table, columns in {
        "api_clients": ["club_id"],
        "api_links": ["client_id"],
        "api_grants": ["client_id", "person_id"],
        "api_webhook_endpoints": ["client_id"],
    }.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade():
    for table in (
        "api_webhook_deliveries",
        "api_webhook_endpoints",
        "api_changes",
        "api_rate_limits",
        "api_idempotency",
        "api_grants",
        "api_links",
        "api_clients",
    ):
        op.drop_table(table)
