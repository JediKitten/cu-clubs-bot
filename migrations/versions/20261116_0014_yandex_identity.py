"""Single-use browser login and Telegram-confirmed Yandex identities."""

import sqlalchemy as sa
from alembic import op

revision = "20261116_0014"
down_revision = "20261116_0013"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "yandex_challenges",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("full_name", sa.String(300), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("state_digest", sa.String(64), unique=True),
        sa.Column("browser_digest", sa.String(64)),
        sa.Column("subject", sa.String(100)),
        sa.Column("email", sa.String(254)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_yandex_challenges_telegram_id", "yandex_challenges", ["telegram_id"])
    op.create_table(
        "yandex_identities",
        sa.Column("subject", sa.String(100), primary_key=True),
        sa.Column("person_id", sa.Uuid(), sa.ForeignKey("people.id"), unique=True, nullable=False),
        sa.Column("email", sa.String(254), unique=True, nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("yandex_identities")
    op.drop_table("yandex_challenges")
