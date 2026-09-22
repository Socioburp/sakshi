"""briefs.root_brief_id: which creative a version belongs to

Revision ID: 0014
Revises: 0013

A copy revision made a child brief (parent set, version + 1) but a picture
revision saved a brand-new root, so a creative's versions were two or more
unrelated chains and "how many times has the owner asked for a change on
this one" could not be answered. Every brief now names its root: itself for
a first version, the parent's root for a revision. The backfill walks the
parents that were recorded, so the chains that exist are stitched; a brief
whose parent link was never written stays a root of its own, which is all
the old data can honestly say.
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

UP = """
ALTER TABLE briefs ADD COLUMN root_brief_id UUID;
CREATE INDEX ix_briefs_root_brief_id ON briefs (root_brief_id);
"""

BACKFILL = """
WITH RECURSIVE chain AS (
    SELECT id, id AS root_id
    FROM briefs
    WHERE parent_brief_id IS NULL
    UNION ALL
    SELECT b.id, c.root_id
    FROM briefs b
    JOIN chain c ON b.parent_brief_id = c.id
)
UPDATE briefs
SET root_brief_id = chain.root_id
FROM chain
WHERE briefs.id = chain.id AND briefs.root_brief_id IS NULL;
"""

DOWN = """
DROP INDEX IF EXISTS ix_briefs_root_brief_id;
ALTER TABLE briefs DROP COLUMN IF EXISTS root_brief_id;
"""


def upgrade() -> None:
    op.execute(UP)
    op.execute(BACKFILL)


def downgrade() -> None:
    op.execute(DOWN)
