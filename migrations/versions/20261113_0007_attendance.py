"""Purpose-bound QR, attendance snapshots and durable import previews.

Revision ID: 20261113_0007
Revises: 20261113_0006
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261113_0007"
down_revision = "20261113_0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "attendance_qr_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("purpose", sa.String(12), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id")),
        sa.Column("person_id", sa.Uuid(), sa.ForeignKey("people.id")),
        sa.Column("created_by_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("purpose IN ('event','personal')", name="ck_attendance_qr_purpose"),
    )
    op.create_index("ix_attendance_qr_tokens_event_id", "attendance_qr_tokens", ["event_id"])
    op.create_index("ix_attendance_qr_tokens_person_id", "attendance_qr_tokens", ["person_id"])
    op.create_table(
        "attendance_qr_grants",
        sa.Column("qr_id", sa.Uuid(), sa.ForeignKey("attendance_qr_tokens.id"), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "attendance_imports",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("actor_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(12), nullable=False, server_default="preview"),
        sa.Column("rows", postgresql.JSONB(), nullable=False),
        sa.Column(
            "issues", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "resolved_rows",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("result", postgresql.JSONB()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('preview','confirmed')", name="ck_attendance_import_status"),
        sa.CheckConstraint("version > 0", name="ck_attendance_import_version"),
    )
    op.create_index("ix_attendance_imports_event_id", "attendance_imports", ["event_id"])
    op.create_table(
        "attendances",
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id"), primary_key=True),
        sa.Column("person_id", sa.Uuid(), sa.ForeignKey("people.id"), primary_key=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("actor_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("full_name_at_mark", sa.String(300), nullable=False),
        sa.Column("time_login_at_mark", sa.String(100)),
        sa.Column("telegram_tag_at_mark", sa.String(64)),
        sa.Column("guest_at_mark", sa.Boolean(), nullable=False),
        sa.Column("was_bot_registered", sa.Boolean(), nullable=False),
        sa.Column("was_registered", sa.Boolean(), nullable=False),
        sa.Column("registered_at_start", sa.Boolean()),
        sa.Column("import_id", sa.Uuid(), sa.ForeignKey("attendance_imports.id")),
        sa.Column(
            "checked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "source IN ('event_qr','personal_qr','manual','import')", name="ck_attendance_source"
        ),
    )


def downgrade():
    op.drop_table("attendances")
    op.drop_index("ix_attendance_imports_event_id", table_name="attendance_imports")
    op.drop_table("attendance_imports")
    op.drop_table("attendance_qr_grants")
    op.drop_index("ix_attendance_qr_tokens_event_id", table_name="attendance_qr_tokens")
    op.drop_index("ix_attendance_qr_tokens_person_id", table_name="attendance_qr_tokens")
    op.drop_table("attendance_qr_tokens")
