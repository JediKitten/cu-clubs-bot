"""Durable calendar reconciliation state, without altering the workbook.

Revision ID: 20261113_0006
Revises: 20261113_0005
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261113_0006"
down_revision = "20261113_0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "calendar_bindings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("spreadsheet_id", sa.String(200), nullable=False),
        sa.Column("sheet_id", sa.Integer()),
        sa.Column("sheet_title", sa.String(100)),
        sa.Column("row_index", sa.Integer()),
        sa.Column("column_index", sa.Integer()),
        sa.Column("baseline_text", sa.Text()),
        sa.Column("baseline_room", sa.String(300)),
        sa.Column("baseline_date", sa.Date()),
        sa.Column("baseline_digest", sa.Boolean()),
        sa.Column(
            "digest_weeks",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("event_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("error_code", sa.String(100)),
        sa.Column("conflict", postgresql.JSONB()),
        sa.Column("resolution", postgresql.JSONB()),
        sa.Column("pending_write", postgresql.JSONB()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_sync_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("event_id", "spreadsheet_id", name="uq_calendar_event_book"),
        sa.CheckConstraint(
            "status IN ('pending','synced','conflict','error')", name="ck_calendar_status"
        ),
        sa.CheckConstraint("revision > 0 AND attempts >= 0", name="ck_calendar_counters"),
    )
    op.create_index("ix_calendar_due", "calendar_bindings", ["next_sync_at", "status"])


def downgrade():
    op.drop_index("ix_calendar_due", table_name="calendar_bindings")
    op.drop_table("calendar_bindings")
