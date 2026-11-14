"""Versioned questionnaires, immediate ratings and resumable responses.

Revision ID: 20261114_0008
Revises: 20261113_0007
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261114_0008"
down_revision = "20261113_0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "survey_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("club_id", sa.Uuid(), sa.ForeignKey("clubs.id")),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id")),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("(club_id IS NULL) <> (event_id IS NULL)", name="ck_survey_scope"),
        sa.CheckConstraint("version > 0", name="ck_survey_version"),
        sa.UniqueConstraint("club_id", "version", name="uq_survey_club_version"),
        sa.UniqueConstraint("event_id", "version", name="uq_survey_event_version"),
    )
    op.create_index("ix_survey_versions_club_id", "survey_versions", ["club_id"])
    op.create_index("ix_survey_versions_event_id", "survey_versions", ["event_id"])
    op.create_table(
        "survey_questions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "survey_version_id", sa.Uuid(), sa.ForeignKey("survey_versions.id"), nullable=False
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("scale_max", sa.Integer()),
        sa.UniqueConstraint("survey_version_id", "position", name="uq_survey_question_position"),
        sa.UniqueConstraint("id", "survey_version_id", name="uq_survey_question_version"),
        sa.CheckConstraint("position >= 0", name="ck_survey_question_position"),
        sa.CheckConstraint(
            "kind IN ('rating','text','single','multiple','scale')", name="ck_survey_question_kind"
        ),
        sa.CheckConstraint("scale_max IS NULL OR scale_max >= 2", name="ck_survey_question_scale"),
        sa.CheckConstraint(
            "(position = 0 AND kind = 'rating' AND prompt = 'Оцените прошедшую встречу от 1 до 5' "
            "AND scale_max IS NOT NULL AND scale_max = 5 AND required) "
            "OR (position > 0 AND kind <> 'rating')",
            name="ck_survey_first_question",
        ),
    )
    op.create_index(
        "ix_survey_questions_survey_version_id", "survey_questions", ["survey_version_id"]
    )
    op.create_table(
        "survey_choices",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("question_id", sa.Uuid(), sa.ForeignKey("survey_questions.id"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(300), nullable=False),
        sa.UniqueConstraint("question_id", "position", name="uq_survey_choice_position"),
    )
    op.create_index("ix_survey_choices_question_id", "survey_choices", ["question_id"])
    op.create_table(
        "survey_responses",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("person_id", sa.Uuid(), sa.ForeignKey("people.id"), nullable=False),
        sa.Column(
            "survey_version_id", sa.Uuid(), sa.ForeignKey("survey_versions.id"), nullable=False
        ),
        sa.Column("rating", sa.Integer()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("event_id", "person_id", name="uq_survey_response_person_event"),
        sa.UniqueConstraint("id", "survey_version_id", name="uq_survey_response_version"),
        sa.CheckConstraint(
            "rating IS NULL OR rating BETWEEN 1 AND 5", name="ck_survey_response_rating"
        ),
    )
    op.create_index("ix_survey_responses_event_id", "survey_responses", ["event_id"])
    op.create_index("ix_survey_responses_person_id", "survey_responses", ["person_id"])
    op.create_table(
        "survey_answers",
        sa.Column("response_id", sa.Uuid(), sa.ForeignKey("survey_responses.id"), primary_key=True),
        sa.Column("question_id", sa.Uuid(), sa.ForeignKey("survey_questions.id"), primary_key=True),
        sa.Column("value", postgresql.JSONB()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade():
    for table in (
        "survey_answers",
        "survey_responses",
        "survey_choices",
        "survey_questions",
        "survey_versions",
    ):
        op.drop_table(table)
