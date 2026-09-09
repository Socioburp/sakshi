"""logo analysis on brands, explicit approval on creatives

Revision ID: 0003
Revises: 0002

`creatives.approved_at` exists so that publishing to a client's Instagram
account is gated on a column set by the client's own tap -- not on the agent
having decided, mid-conversation, that they sounded happy. An agent can be
talked into "yes"; a NULL timestamp cannot.
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

UP = """
ALTER TABLE brands ADD COLUMN logo_notes text;
ALTER TABLE brands ADD COLUMN logo_analysis jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE creatives ADD COLUMN approved_at timestamptz;
ALTER TABLE creatives ADD COLUMN approved_via varchar(24);
CREATE INDEX ix_creatives_approved_at ON creatives(approved_at)
    WHERE approved_at IS NOT NULL;
"""

DOWN = """
DROP INDEX IF EXISTS ix_creatives_approved_at;
ALTER TABLE creatives DROP COLUMN IF EXISTS approved_via;
ALTER TABLE creatives DROP COLUMN IF EXISTS approved_at;
ALTER TABLE brands DROP COLUMN IF EXISTS logo_analysis;
ALTER TABLE brands DROP COLUMN IF EXISTS logo_notes;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
