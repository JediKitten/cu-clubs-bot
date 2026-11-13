"""Active template names and durable default duration for copied drafts.

Revision ID: 20261113_0005
Revises: 20261112_0004
"""

import sqlalchemy as sa
from alembic import op

revision = "20261113_0005"
down_revision = "20261112_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events", sa.Column("template_duration_minutes", sa.Integer(), nullable=True))
    op.add_column("events", sa.Column("template_start_time", sa.String(5), nullable=True))
    op.create_check_constraint(
        "ck_event_template_duration",
        "events",
        "template_duration_minutes IS NULL OR template_duration_minutes > 0",
    )
    op.create_check_constraint(
        "ck_event_template_start_time",
        "events",
        "template_start_time IS NULL OR template_start_time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'",
    )
    op.drop_constraint("uq_event_template_name", "event_templates", type_="unique")
    op.create_index(
        "uq_event_template_active_name",
        "event_templates",
        ["club_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_check_constraint("ck_event_template_version", "event_templates", "version > 0")


def downgrade() -> None:
    # A live name may have been reused after deletion. The old schema cannot
    # represent that history: refuse this downgrade instead of discarding or
    # silently renaming preserved templates.
    op.drop_constraint("ck_event_template_version", "event_templates", type_="check")
    op.drop_index("uq_event_template_active_name", table_name="event_templates")
    op.create_unique_constraint("uq_event_template_name", "event_templates", ["club_id", "name"])
    op.drop_constraint("ck_event_template_duration", "events", type_="check")
    op.drop_constraint("ck_event_template_start_time", "events", type_="check")
    op.drop_column("events", "template_duration_minutes")
    op.drop_column("events", "template_start_time")
