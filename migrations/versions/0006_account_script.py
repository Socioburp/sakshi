"""accounts.script: the script the owner TYPES in, kept apart from their language

Revision ID: 0006
Revises: 0005

`accounts.locale` records which language the owner uses ("hi-IN"). It cannot
record which script they read it in, and the two are different facts: a
Hindi speaker who types "kal se sale hai" reads Latin letters; one who types
in Devanagari does not want a romanised reply.

A voice note tells us the language (the transcript) but nothing about the
script -- the transcript's script is the vendor's choice. So script is
locked only from typed messages, and this column is where it lives. NULL
means "never typed in an Indic language"; the reply then defaults to Latin,
which is what an Indian phone keyboard produces.
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE accounts ADD COLUMN script VARCHAR(16)")


def downgrade() -> None:
    op.execute("ALTER TABLE accounts DROP COLUMN script")
