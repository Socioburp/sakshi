"""creatives.video_key / video_url: the reel rendered from a creative

Revision ID: 0012
Revises: 0011

A reel is the approved still set in motion, so it hangs off the creative
row rather than being a creative of its own: same brief, same background,
same composed card, plus the MP4 the compositor's PNG was animated into.
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

UP = """
ALTER TABLE creatives ADD COLUMN video_key TEXT;
ALTER TABLE creatives ADD COLUMN video_url TEXT;
"""

DOWN = """
ALTER TABLE creatives DROP COLUMN IF EXISTS video_url;
ALTER TABLE creatives DROP COLUMN IF EXISTS video_key;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
