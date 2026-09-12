"""creatives.billed, and the index the job reaper needs

Revision ID: 0007
Revises: 0006

`creatives.billed` records whether THIS slide was paid for. The pipeline knew
that at charge time and then forgot it, so a reaper that finds a creative
stuck in "generating" after a worker crash had no way to know whether a
refund was owed. Now it does: refund exactly the billed ones, once.

The partial index on jobs(status, started_at) is what the reaper scans every
30 seconds; without it that scan is a sequential read of the whole audit
trail.
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE creatives ADD COLUMN billed BOOLEAN NOT NULL DEFAULT FALSE")
    op.execute(
        "CREATE INDEX ix_jobs_open ON jobs(status, started_at) "
        "WHERE status IN ('queued','running')"
    )
    op.execute(
        "CREATE INDEX ix_creatives_open ON creatives(status, created_at) "
        "WHERE status IN ('generating','composing')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_creatives_open")
    op.execute("DROP INDEX IF EXISTS ix_jobs_open")
    op.execute("ALTER TABLE creatives DROP COLUMN billed")
