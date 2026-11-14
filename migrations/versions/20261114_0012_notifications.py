"""Persist explicitly confirmed event broadcasts."""

import sqlalchemy as sa
from alembic import op

revision = "20261114_0012"
down_revision = "20261114_0011"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "notification_broadcasts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("actor_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("audience", sa.String(20), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("audience IN ('registered','attended')", name="ck_broadcast_audience"),
        sa.CheckConstraint("length(body) BETWEEN 1 AND 500", name="ck_broadcast_body"),
    )
    op.create_index("ix_notification_broadcasts_event_id", "notification_broadcasts", ["event_id"])


def downgrade():
    op.drop_table("notification_broadcasts")
