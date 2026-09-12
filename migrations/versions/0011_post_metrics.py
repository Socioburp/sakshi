"""post_metrics and ig_account_stats: what each post did on Instagram

Revision ID: 0011
Revises: 0010

The taste profile learns from the owner's taps; this learns from their
followers. One row per Instagram post the account has (ours or their own),
refreshed from the Insights API while the post is young, and one row per
account per day for the reach and follower count the weekly readout needs.
`facts` carries the brief's shape (template, aspect, format, intent, mood,
own photo or not) captured at publish, so a post's numbers can be traced to
the choices that made it.
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

UP = """
CREATE TABLE post_metrics (
    id                  UUID PRIMARY KEY,
    brand_id            UUID NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    publication_id      UUID REFERENCES publications(id) ON DELETE SET NULL,
    ig_media_id         VARCHAR(64) NOT NULL,
    media_type          VARCHAR(24) NOT NULL DEFAULT 'IMAGE',
    media_product_type  VARCHAR(24),
    permalink           TEXT,
    caption             TEXT,
    posted_at           TIMESTAMPTZ,
    reach               INTEGER NOT NULL DEFAULT 0,
    views               INTEGER NOT NULL DEFAULT 0,
    likes               INTEGER NOT NULL DEFAULT 0,
    comments            INTEGER NOT NULL DEFAULT 0,
    saved               INTEGER NOT NULL DEFAULT 0,
    shares              INTEGER NOT NULL DEFAULT 0,
    follows             INTEGER NOT NULL DEFAULT 0,
    profile_visits      INTEGER NOT NULL DEFAULT 0,
    total_interactions  INTEGER NOT NULL DEFAULT 0,
    facts               JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    synced_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_post_metrics_brand_media UNIQUE (brand_id, ig_media_id)
);
CREATE INDEX ix_post_metrics_brand_posted ON post_metrics (brand_id, posted_at);

CREATE TABLE ig_account_stats (
    id                  UUID PRIMARY KEY,
    brand_id            UUID NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    day                 DATE NOT NULL,
    followers           INTEGER,
    media_count         INTEGER,
    reach               INTEGER NOT NULL DEFAULT 0,
    accounts_engaged    INTEGER NOT NULL DEFAULT 0,
    total_interactions  INTEGER NOT NULL DEFAULT 0,
    raw                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_ig_account_stats_brand_day UNIQUE (brand_id, day)
);
"""

DOWN = """
DROP TABLE IF EXISTS ig_account_stats;
DROP TABLE IF EXISTS post_metrics;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
