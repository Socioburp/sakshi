"""carry the parts of the v1 schema that were worth keeping

Revision ID: 0004
Revises: 0003

Everything here is lifted from the previous bot's database, fixed on the way in.

* `industry_research` replaces `industry_style_research`. Same idea -- research
  an industry once, reuse it for every brand in it -- but keyed on a normalised
  slug instead of the client's own words. The old table's primary key was free
  text ("handmade gifting business"), so almost every lookup missed and almost
  every write created a new row. A cache with a ~0% hit rate is just a bill.

* `learning_events` and `analytics_events` come across nearly unchanged. They
  answer different questions and should stay separate: one is "did this
  creative teach us anything", the other is "where do clients drop off".

* `ad_accounts` replaces four columns that were bolted onto the businesses
  table. Partner access is a state machine with its own timestamps; that is a
  row, not a column on a table about something else.

* `accounts.pending_first_request` is a genuinely good idea from v1: when the
  first message already describes a real creative, hold it through onboarding
  and generate it the moment setup completes, so the client never repeats
  themselves.
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

UP = """
ALTER TABLE accounts  ADD COLUMN pending_first_request text;
ALTER TABLE brands    ADD COLUMN industry_slug varchar(60);
ALTER TABLE creatives ADD COLUMN quality_score integer;
CREATE INDEX ix_brands_industry_slug ON brands(industry_slug);

CREATE TABLE industry_research (
    industry_slug varchar(60) PRIMARY KEY,
    label         varchar(120),
    style_summary text NOT NULL,
    sources       text[] NOT NULL DEFAULT '{}',
    refreshed_at  timestamptz NOT NULL DEFAULT now(),
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE learning_events (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id      uuid NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    creative_id   uuid REFERENCES creatives(id) ON DELETE SET NULL,
    event_type    varchar(32) NOT NULL,
    quality_score integer,
    meta          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_learning_events_type CHECK (event_type IN (
        'recorded','skipped_quality','skipped_no_profile','skipped_free_revision',
        'rejected_by_client','distilled'))
);
CREATE INDEX ix_learning_events_brand_id ON learning_events(brand_id);

CREATE TABLE analytics_events (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id     uuid NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    event_type     varchar(48) NOT NULL,
    event_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_analytics_events_type CHECK (event_type IN (
        'signup','onboarding_completed','logo_received','first_creative_shown',
        'first_creative_approved','first_published','returned_voluntarily',
        'topup','churn_risk'))
);
CREATE INDEX ix_analytics_events_account_id ON analytics_events(account_id);
CREATE INDEX ix_analytics_events_event_type ON analytics_events(event_type);

CREATE TABLE ad_accounts (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id              uuid NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    meta_ad_account_id    varchar(64),
    meta_business_id      varchar(64),
    partner_access_status varchar(24) NOT NULL DEFAULT 'not_connected',
    verified_at           timestamptz,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_ad_accounts_status CHECK (partner_access_status IN (
        'not_connected','pending_approval','granted','revoked'))
);
CREATE INDEX ix_ad_accounts_brand_id ON ad_accounts(brand_id);
"""

DOWN = """
DROP TABLE IF EXISTS ad_accounts;
DROP TABLE IF EXISTS analytics_events;
DROP TABLE IF EXISTS learning_events;
DROP TABLE IF EXISTS industry_research;
DROP INDEX IF EXISTS ix_brands_industry_slug;
ALTER TABLE creatives DROP COLUMN IF EXISTS quality_score;
ALTER TABLE brands    DROP COLUMN IF EXISTS industry_slug;
ALTER TABLE accounts  DROP COLUMN IF EXISTS pending_first_request;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
