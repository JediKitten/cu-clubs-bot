"""Persistent meetings, collaboration, registrations and templates.

Revision ID: 20261112_0003
Revises: 20261112_0002

DDL is frozen here; it does not import mutable application models at runtime.
"""

from alembic import op

revision = "20261112_0003"
down_revision = "20261112_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE events (
	id UUID NOT NULL,
	public_id VARCHAR(32) NOT NULL,
	initiator_club_id UUID NOT NULL,
	author_person_id UUID NOT NULL,
	last_editor_telegram_id BIGINT NOT NULL,
	title VARCHAR(150) DEFAULT '' NOT NULL,
	description TEXT DEFAULT '' NOT NULL,
	post_registration_text TEXT DEFAULT '' NOT NULL,
	status VARCHAR(30) DEFAULT 'draft' NOT NULL,
	visibility VARCHAR(10) DEFAULT 'open' NOT NULL,
	starts_at TIMESTAMP WITH TIME ZONE,
	ends_at TIMESTAMP WITH TIME ZONE,
	registration_deadline TIMESTAMP WITH TIME ZONE,
	capacity INTEGER,
	expected_attendance INTEGER,
	venue_type VARCHAR(20) DEFAULT 'campus' NOT NULL,
	room VARCHAR(300),
	venue_details TEXT DEFAULT '' NOT NULL,
	needs_talk BOOLEAN DEFAULT 'false' NOT NULL,
	needs_microphones BOOLEAN DEFAULT 'false' NOT NULL,
	version INTEGER DEFAULT '1' NOT NULL,
	start_registration_snapshot JSONB,
	snapshot_starts_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_event_status CHECK (status IN
        ('draft','pending_partner','published','cancelled','held')),
	CONSTRAINT ck_event_visibility CHECK (visibility IN ('open','closed')),
	CONSTRAINT ck_event_version CHECK (version > 0),
	CONSTRAINT ck_event_capacity CHECK (capacity IS NULL OR capacity > 0),
	CONSTRAINT ck_event_expected CHECK (expected_attendance IS NULL OR expected_attendance > 0),
	CONSTRAINT ck_event_time CHECK (ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at),
	CONSTRAINT ck_event_deadline CHECK (registration_deadline IS NULL
        OR starts_at IS NULL OR registration_deadline <= starts_at),
	CONSTRAINT ck_event_venue CHECK (venue_type IN ('campus','external','online')),
	UNIQUE (public_id),
	FOREIGN KEY(initiator_club_id) REFERENCES clubs (id),
	FOREIGN KEY(author_person_id) REFERENCES people (id)
)""")
    op.execute("""CREATE INDEX ix_event_calendar ON events (status, visibility, starts_at)""")
    op.execute("""CREATE TABLE event_clubs (
	event_id UUID NOT NULL,
	club_id UUID NOT NULL,
	role VARCHAR(20) NOT NULL,
	PRIMARY KEY (event_id, club_id),
	CONSTRAINT ck_event_club_role CHECK (role IN ('initiator','partner')),
	FOREIGN KEY(event_id) REFERENCES events (id),
	FOREIGN KEY(club_id) REFERENCES clubs (id)
)""")
    op.execute("""CREATE TABLE event_invitations (
	id UUID NOT NULL,
	event_id UUID NOT NULL,
	partner_club_id UUID NOT NULL,
	version INTEGER NOT NULL,
	status VARCHAR(20) DEFAULT 'pending' NOT NULL,
	decided_by_telegram_id BIGINT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	decided_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT uq_event_invitation_version UNIQUE (event_id, version),
	CONSTRAINT ck_event_invitation_status CHECK (status IN
        ('pending','accepted','declined','superseded')),
	FOREIGN KEY(event_id) REFERENCES events (id),
	FOREIGN KEY(partner_club_id) REFERENCES clubs (id)
)""")
    op.execute("""CREATE INDEX ix_event_invitations_event_id ON event_invitations (event_id)""")
    op.execute("""CREATE TABLE registrations (
	event_id UUID NOT NULL,
	person_id UUID NOT NULL,
	status VARCHAR(20) DEFAULT 'active' NOT NULL,
	registered_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	cancelled_at TIMESTAMP WITH TIME ZONE,
	source VARCHAR(20) DEFAULT 'telegram' NOT NULL,
	PRIMARY KEY (event_id, person_id),
	CONSTRAINT ck_registration_status CHECK (status IN ('active','cancelled')),
	FOREIGN KEY(event_id) REFERENCES events (id),
	FOREIGN KEY(person_id) REFERENCES people (id)
)""")
    op.execute("""CREATE INDEX ix_registration_person ON registrations (person_id, status)""")
    op.execute("""CREATE TABLE registration_history (
	id UUID NOT NULL,
	event_id UUID NOT NULL,
	person_id UUID NOT NULL,
	status VARCHAR(20) NOT NULL,
	changed_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(event_id) REFERENCES events (id),
	FOREIGN KEY(person_id) REFERENCES people (id)
)""")
    op.execute(
        """CREATE INDEX ix_registration_history_event_id ON registration_history (event_id)"""
    )
    op.execute("""CREATE TABLE event_templates (
	id UUID NOT NULL,
	public_id VARCHAR(32) NOT NULL,
	club_id UUID NOT NULL,
	name VARCHAR(150) NOT NULL,
	fields JSONB DEFAULT '{}'::jsonb NOT NULL,
	version INTEGER DEFAULT '1' NOT NULL,
	deleted_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_event_template_name UNIQUE (club_id, name),
	UNIQUE (public_id),
	FOREIGN KEY(club_id) REFERENCES clubs (id)
)""")


def downgrade() -> None:
    op.drop_table("event_templates")
    op.drop_table("registration_history")
    op.drop_table("registrations")
    op.drop_table("event_invitations")
    op.drop_table("event_clubs")
    op.drop_table("events")
