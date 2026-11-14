"""Immutable questionnaire versions and one resumable response per visit."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cu_clubs.models import Base

RATING_PROMPT = "Оцените прошедшую встречу от 1 до 5"


class SurveyVersion(Base):
    __tablename__ = "survey_versions"
    __table_args__ = (
        CheckConstraint("(club_id IS NULL) <> (event_id IS NULL)", name="ck_survey_scope"),
        CheckConstraint("version > 0", name="ck_survey_version"),
        UniqueConstraint("club_id", "version", name="uq_survey_club_version"),
        UniqueConstraint("event_id", "version", name="uq_survey_event_version"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    club_id: Mapped[UUID | None] = mapped_column(ForeignKey("clubs.id"), index=True)
    event_id: Mapped[UUID | None] = mapped_column(ForeignKey("events.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SurveyQuestion(Base):
    __tablename__ = "survey_questions"
    __table_args__ = (
        UniqueConstraint("survey_version_id", "position", name="uq_survey_question_position"),
        UniqueConstraint("id", "survey_version_id", name="uq_survey_question_version"),
        CheckConstraint("position >= 0", name="ck_survey_question_position"),
        CheckConstraint(
            "kind IN ('rating','text','single','multiple','scale')", name="ck_survey_question_kind"
        ),
        CheckConstraint("scale_max IS NULL OR scale_max >= 2", name="ck_survey_question_scale"),
        CheckConstraint(
            "(position = 0 AND kind = 'rating' AND prompt = 'Оцените прошедшую встречу от 1 до 5' "
            "AND scale_max IS NOT NULL AND scale_max = 5 AND required) "
            "OR (position > 0 AND kind <> 'rating')",
            name="ck_survey_first_question",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    survey_version_id: Mapped[UUID] = mapped_column(ForeignKey("survey_versions.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(12))
    prompt: Mapped[str] = mapped_column(Text)
    required: Mapped[bool] = mapped_column(Boolean)
    scale_max: Mapped[int | None] = mapped_column(Integer)


class SurveyChoice(Base):
    __tablename__ = "survey_choices"
    __table_args__ = (
        UniqueConstraint("question_id", "position", name="uq_survey_choice_position"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    question_id: Mapped[UUID] = mapped_column(ForeignKey("survey_questions.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(300))


class SurveyResponse(Base):
    __tablename__ = "survey_responses"
    __table_args__ = (
        UniqueConstraint("event_id", "person_id", name="uq_survey_response_person_event"),
        UniqueConstraint("id", "survey_version_id", name="uq_survey_response_version"),
        CheckConstraint(
            "rating IS NULL OR rating BETWEEN 1 AND 5", name="ck_survey_response_rating"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), index=True)
    person_id: Mapped[UUID] = mapped_column(ForeignKey("people.id"), index=True)
    survey_version_id: Mapped[UUID] = mapped_column(ForeignKey("survey_versions.id"))
    rating: Mapped[int | None] = mapped_column(Integer)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SurveyAnswer(Base):
    __tablename__ = "survey_answers"
    response_id: Mapped[UUID] = mapped_column(ForeignKey("survey_responses.id"), primary_key=True)
    question_id: Mapped[UUID] = mapped_column(ForeignKey("survey_questions.id"), primary_key=True)
    value: Mapped[object | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
