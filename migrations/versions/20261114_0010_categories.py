"""Temporary self-declared categories; independent of email verification."""

import sqlalchemy as sa
from alembic import op

revision = "20261114_0010"
down_revision = "20261114_0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("people", sa.Column("declared_category", sa.String(20), nullable=True))
    op.add_column(
        "people", sa.Column("category_selected_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "ck_person_declared_category",
        "people",
        "declared_category IS NULL OR "
        "declared_category IN ('student','staff','applicant','external')",
    )
    op.create_check_constraint(
        "ck_person_category_selected",
        "people",
        "(declared_category IS NULL) = (category_selected_at IS NULL)",
    )
    op.add_column("attendances", sa.Column("category_at_mark", sa.String(20), nullable=True))


def downgrade():
    op.drop_column("attendances", "category_at_mark")
    op.drop_constraint("ck_person_category_selected", "people", type_="check")
    op.drop_constraint("ck_person_declared_category", "people", type_="check")
    op.drop_column("people", "category_selected_at")
    op.drop_column("people", "declared_category")
