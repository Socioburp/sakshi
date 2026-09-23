"""creative_events: a 'quality' kind, for the verdict on the finished card

Revision ID: 0015
Revises: 0014

Every creative now passes a final check before it is delivered: deterministic
metrics on the exported frame, then a vision inspector looking at the JPEG the
client actually receives. That produces a verdict and a score, and both are
worth keeping.

The score already has a home -- creatives.quality_score has existed since 0004
and no code has ever written it. The verdict does not, and it is the more
useful of the two: the owner's own answer to a creative is already recorded
here as 'approve', 'change_words', 'change_picture', 'revise' and 'regenerate',
so storing what the machine thought beside what the owner did is what lets the
thresholds be calibrated against approvals later instead of against a guess.
That is the whole of "the product must get smarter over time": it cannot
learn from judgements it threw away.

The kind is 'quality' rather than something narrower because this is the row
for what the product thought of its own work, whoever ends up writing it.
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

UP = """
ALTER TABLE creative_events DROP CONSTRAINT ck_creative_events_kind;
ALTER TABLE creative_events ADD CONSTRAINT ck_creative_events_kind CHECK (kind IN (
    'created','approve','change_words','change_picture','revise',
    'regenerate','publish','suggested','suggestion_taken','quality'));
"""

DOWN = """
DELETE FROM creative_events WHERE kind = 'quality';
ALTER TABLE creative_events DROP CONSTRAINT ck_creative_events_kind;
ALTER TABLE creative_events ADD CONSTRAINT ck_creative_events_kind CHECK (kind IN (
    'created','approve','change_words','change_picture','revise',
    'regenerate','publish','suggested','suggestion_taken'));
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
