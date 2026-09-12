"""content_plans: a month of posts, decided before the month starts

Revision ID: 0010
Revises: 0009

An agency does not think one post at a time. It plans the month: a goal
(footfall, leads, a launch, awareness), a pillar mix, a cadence, the
festival arcs (teaser, launch, last day), and then fills the calendar. This
table holds that plan per brand per month. Slots are JSONB on purpose: a
plan is rewritten whole when the owner changes the goal, and read whole when
the day's idea is chosen.
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

UP = """
CREATE TABLE content_plans (
    id          UUID PRIMARY KEY,
    brand_id    UUID NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    month       DATE NOT NULL,
    goal        VARCHAR(24) NOT NULL DEFAULT 'awareness',
    cadence     SMALLINT NOT NULL DEFAULT 4,
    pillar_mix  JSONB NOT NULL DEFAULT '{}'::jsonb,
    slots       JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_content_plans_goal CHECK (goal IN ('footfall','leads','launch','awareness')),
    CONSTRAINT ck_content_plans_cadence CHECK (cadence BETWEEN 1 AND 7),
    CONSTRAINT uq_content_plans_brand_month UNIQUE (brand_id, month)
);
"""

DOWN = """
DROP TABLE IF EXISTS content_plans;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
