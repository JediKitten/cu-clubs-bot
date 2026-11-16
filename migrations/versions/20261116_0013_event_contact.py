"""Explicit optional public meeting contact, never inferred from personal data."""

import sqlalchemy as sa
from alembic import op

revision = "20261116_0013"
down_revision = "20261114_0012"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "events", sa.Column("public_contact", sa.String(300), server_default="", nullable=False)
    )


def downgrade():
    op.drop_column("events", "public_contact")
