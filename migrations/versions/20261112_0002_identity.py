"""Durable email verification challenges.

Revision ID: 20261112_0002
Revises: 20261112_0001
"""

import sqlalchemy as sa
from alembic import op

revision = "20261112_0002"
down_revision = "20261112_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_challenges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("time_login", sa.String(100), nullable=False),
        sa.Column("full_name", sa.String(300), nullable=False),
        sa.Column("code_digest", sa.String(64), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("attempts >= 0 AND attempts <= 5", name="ck_email_challenge_attempts"),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_email_challenge_account_created", "email_challenges", ["telegram_id", "created_at"]
    )
    op.create_index(
        "ix_email_challenge_login_created", "email_challenges", ["time_login", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_email_challenge_login_created", table_name="email_challenges")
    op.drop_index("ix_email_challenge_account_created", table_name="email_challenges")
    op.drop_table("email_challenges")
