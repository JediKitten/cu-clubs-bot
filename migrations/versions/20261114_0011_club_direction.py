"""Add a public direction label to club profiles."""

import sqlalchemy as sa
from alembic import op

revision = "20261114_0011"
down_revision = "20261114_0010"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("clubs", sa.Column("direction", sa.String(100), nullable=True))


def downgrade():
    op.drop_column("clubs", "direction")
