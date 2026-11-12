"""Initial people clubs permissions inbox outbox and audit

Revision ID: 20261112_0001
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261112_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_log_entity_id"), "audit_log", ["entity_id"], unique=False)
    op.create_table(
        "club_levels",
        sa.Column("name", sa.String(length=30), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("name"),
        sa.UniqueConstraint("sort_order"),
    )
    op.create_table(
        "conversations",
        sa.Column("telegram_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("state", sa.String(length=100), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("telegram_id"),
    )
    op.create_table(
        "outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=100), nullable=False),
        sa.Column("recipient_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dedupe_key", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending','processing','sent','failed','uncertain')",
            name="ck_outbox_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_outbox_attempts"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index(
        "ix_outbox_ready", "outbox", ["status", "available_at", "lease_until"], unique=False
    )
    op.create_table(
        "people",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("full_name", sa.String(length=300), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=True),
        sa.Column("time_login", sa.String(length=100), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("time_login"),
    )
    op.create_table(
        "telegram_updates",
        sa.Column("update_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','processing','done','failed')", name="ck_telegram_update_status"
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_telegram_update_attempts"),
        sa.PrimaryKeyConstraint("update_id"),
    )
    op.create_index(
        "ix_telegram_update_ready",
        "telegram_updates",
        ["status", "available_at", "lease_until"],
        unique=False,
    )
    op.create_table(
        "clubs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("public_id", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("level_name", sa.String(length=30), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("channel", sa.String(length=500), nullable=True),
        sa.Column("cover", sa.Text(), nullable=True),
        sa.Column(
            "links",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version > 0", name="ck_club_positive_version"),
        sa.ForeignKeyConstraint(
            ["level_name"],
            ["club_levels.name"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
    )
    op.create_table(
        "global_admins",
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["people.id"],
        ),
        sa.PrimaryKeyConstraint("person_id"),
    )
    op.create_table(
        "telegram_accounts",
        sa.Column("telegram_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.CheckConstraint("telegram_id > 0", name="ck_telegram_account_positive_id"),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["people.id"],
        ),
        sa.PrimaryKeyConstraint("telegram_id"),
        sa.UniqueConstraint("person_id"),
    )
    op.create_table(
        "memberships",
        sa.Column("club_id", sa.Uuid(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("edit_profile", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("manage_events", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("record_attendance", sa.Boolean(), server_default="true", nullable=False),
        sa.CheckConstraint("role IN ('leader','organizer')", name="ck_membership_role"),
        sa.ForeignKeyConstraint(
            ["club_id"],
            ["clubs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["people.id"],
        ),
        sa.PrimaryKeyConstraint("club_id", "person_id"),
    )
    op.create_index(
        "uq_membership_one_leader",
        "memberships",
        ["club_id"],
        unique=True,
        postgresql_where=sa.text("role = 'leader'"),
    )
    op.create_table(
        "public_team",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("club_id", sa.Uuid(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("role", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(
            ["club_id"],
            ["clubs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("club_id", "sort_order", name="uq_public_team_position"),
    )
    op.create_index(op.f("ix_public_team_club_id"), "public_team", ["club_id"], unique=False)
    op.create_table(
        "subscriptions",
        sa.Column("club_id", sa.Uuid(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["club_id"],
            ["clubs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["person_id"],
            ["people.id"],
        ),
        sa.PrimaryKeyConstraint("club_id", "person_id"),
    )


def downgrade() -> None:
    op.drop_table("subscriptions")
    op.drop_index(op.f("ix_public_team_club_id"), table_name="public_team")
    op.drop_table("public_team")
    op.drop_index(
        "uq_membership_one_leader",
        table_name="memberships",
        postgresql_where=sa.text("role = 'leader'"),
    )
    op.drop_table("memberships")
    op.drop_table("telegram_accounts")
    op.drop_table("global_admins")
    op.drop_table("clubs")
    op.drop_index("ix_telegram_update_ready", table_name="telegram_updates")
    op.drop_table("telegram_updates")
    op.drop_table("people")
    op.drop_index("ix_outbox_ready", table_name="outbox")
    op.drop_table("outbox")
    op.drop_table("conversations")
    op.drop_table("club_levels")
    op.drop_index(op.f("ix_audit_log_entity_id"), table_name="audit_log")
    op.drop_table("audit_log")
