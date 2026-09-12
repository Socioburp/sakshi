"""ig_events: comments and DMs from Instagram, and the reply the owner approved

Revision ID: 0013
Revises: 0012

Instagram sends a webhook when someone comments on a post or messages the
account. Each becomes a row here: what was said, who said it, the reply
Sakshi drafted, and what the owner did with it (sent as drafted, edited,
skipped). One row per Instagram object; the unique key makes a webhook retry
idempotent.
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

UP = """
CREATE TABLE ig_events (
    id                 UUID PRIMARY KEY,
    brand_id           UUID NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    ig_account_id      UUID REFERENCES ig_accounts(id) ON DELETE SET NULL,
    kind               VARCHAR(16) NOT NULL,
    ig_object_id       VARCHAR(255) NOT NULL,
    parent_id          VARCHAR(255),
    media_id           VARCHAR(64),
    permalink          TEXT,
    from_id            VARCHAR(64),
    from_username      VARCHAR(120),
    text               TEXT,
    draft_reply        TEXT,
    reply_text         TEXT,
    reply_ig_id        VARCHAR(255),
    status             VARCHAR(16) NOT NULL DEFAULT 'new',
    notify_message_id  UUID,
    error              TEXT,
    meta               JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_ig_events_kind CHECK (kind IN ('comment','mention','message')),
    CONSTRAINT ck_ig_events_status CHECK (
        status IN ('new','drafted','approved','sent','skipped','failed','ignored')
    ),
    CONSTRAINT uq_ig_events_brand_object UNIQUE (brand_id, kind, ig_object_id)
);
CREATE INDEX ix_ig_events_brand_status ON ig_events (brand_id, status);
"""

DOWN = """
DROP TABLE IF EXISTS ig_events;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
