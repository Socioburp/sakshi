"""messages.answered_at: which inbound messages a turn has actually answered

Revision ID: 0008
Revises: 0007

Two messages can arrive while one turn is running. The first job answers
what it saw; the second job must decide whether its message was covered.
Inferring that from "is there an outbound row after mine" is wrong in both
directions -- a reply recorded after a message that arrived mid-turn is not
an answer to it -- so coverage is recorded explicitly, by the turn that did
the answering, on every inbound row it folded into its user turn.
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE messages ADD COLUMN answered_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE messages DROP COLUMN answered_at")
