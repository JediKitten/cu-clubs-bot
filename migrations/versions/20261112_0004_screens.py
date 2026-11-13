"""Durable per-chat Telegram screen and original-banner file cache.

Revision ID: 20261112_0004
Revises: 20261112_0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261112_0004"
down_revision = "20261112_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ui_screens",
        sa.Column("bot_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("pages", postgresql.JSONB(), nullable=False),
        sa.Column("base_markup", postgresql.JSONB(), nullable=True),
        sa.Column("page_index", sa.Integer(), server_default="0", nullable=False),
        sa.Column("page_token", sa.String(32), nullable=False),
        sa.Column("photo_file_id", sa.Text(), nullable=True),
        sa.Column("banner_role", sa.String(50), nullable=True),
        sa.Column(
            "banner_cache",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("bot_id", "chat_id"),
        sa.CheckConstraint("kind IN ('text','photo')", name="ck_ui_screen_kind"),
        sa.CheckConstraint("page_index >= 0", name="ck_ui_screen_page_index"),
    )


def downgrade() -> None:
    op.drop_table("ui_screens")
