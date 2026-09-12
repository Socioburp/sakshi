"""creative_events: what the owner did with each creative

Revision ID: 0009
Revises: 0008

Every tap and every revision is a vote. "Post it" is a like; "change the
picture" is a dislike of the image and a like of the words; a regenerate on
slide 3 is a dislike of one picture. Until now those votes were spent the
moment they were cast. This table keeps them, so the product can learn what
each owner approves (app/insights) and, with enough of them, propose the
post before they ask.

Deliberately narrow: one row per event, a small JSONB for the facts that
were true at the time (template, aspect, mood, lane), nothing derived.
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

UP = """
CREATE TABLE creative_events (
    id          UUID PRIMARY KEY,
    account_id  UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    brand_id    UUID NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    brief_id    UUID,
    creative_id UUID,
    kind        VARCHAR(24) NOT NULL,
    meta        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_creative_events_kind CHECK (kind IN (
        'created','approve','change_words','change_picture','revise','regenerate',
        'publish','suggested','suggestion_taken'))
);
CREATE INDEX ix_creative_events_brand_time ON creative_events(brand_id, created_at DESC);
CREATE INDEX ix_creative_events_brief ON creative_events(brief_id);
"""

DOWN = """
DROP TABLE IF EXISTS creative_events;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
