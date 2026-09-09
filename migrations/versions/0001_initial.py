"""initial schema

Revision ID: 0001
Revises:
"""

from alembic import op

from app.config import settings

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

DDL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE accounts (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    wa_phone        varchar(32) NOT NULL UNIQUE,
    display_name    varchar(120),
    locale          varchar(16) NOT NULL DEFAULT 'en-IN',
    plan            varchar(32) NOT NULL DEFAULT 'trial',
    credits_balance integer NOT NULL DEFAULT 0,
    onboarded_at    timestamptz,
    blocked_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_accounts_credits_nonneg CHECK (credits_balance >= 0)
);

-- Brand identity: plain columns, no embedding. Loaded whole into every prompt
-- so a never_say rule cannot be missed by ranking below a threshold.
CREATE TABLE brands (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      uuid NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name            varchar(120) NOT NULL,
    category        varchar(80),
    tagline         varchar(200),
    description     text,
    target_audience text,
    tone            text,
    languages       text[] NOT NULL DEFAULT '{}',
    never_say       text[] NOT NULL DEFAULT '{}',
    always_say      text[] NOT NULL DEFAULT '{}',
    cta_defaults    text[] NOT NULL DEFAULT '{}',
    palette         jsonb NOT NULL DEFAULT '{}'::jsonb,
    fonts           jsonb NOT NULL DEFAULT '{}'::jsonb,
    logo_url        text,
    watermark_url   text,
    template_prefs  jsonb NOT NULL DEFAULT '{}'::jsonb,
    is_default      boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_brands_account_id ON brands(account_id);

CREATE TABLE ig_accounts (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id         uuid NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    ig_user_id       varchar(64) NOT NULL,
    username         varchar(120),
    access_token     text,
    token_expires_at timestamptz,
    scopes           text[] NOT NULL DEFAULT '{}',
    status           varchar(24) NOT NULL DEFAULT 'connected',
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_ig_brand_user UNIQUE (brand_id, ig_user_id)
);
CREATE INDEX ix_ig_accounts_brand_id ON ig_accounts(brand_id);

CREATE TABLE wa_sessions (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id        uuid NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    wa_id             varchar(32) NOT NULL,
    last_inbound_at   timestamptz,
    last_outbound_at  timestamptz,
    window_expires_at timestamptz,
    active_brief_id   uuid,
    state             jsonb NOT NULL DEFAULT '{}'::jsonb,
    closed_at         timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_wa_sessions_account_id ON wa_sessions(account_id);
CREATE INDEX ix_wa_sessions_wa_id ON wa_sessions(wa_id);
CREATE UNIQUE INDEX uq_wa_sessions_live ON wa_sessions(account_id, wa_id)
    WHERE closed_at IS NULL;

CREATE TABLE messages (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id            uuid NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    session_id            uuid REFERENCES wa_sessions(id) ON DELETE SET NULL,
    channel               varchar(24) NOT NULL DEFAULT 'whatsapp',
    provider              varchar(24) NOT NULL,
    provider_message_id   varchar(160),
    direction             varchar(8) NOT NULL,
    kind                  varchar(24) NOT NULL,
    text                  text,
    media_url             text,
    media_mime            varchar(80),
    media_duration_ms     integer,
    transcript            text,
    transcript_provider   varchar(32),
    transcript_lang       varchar(16),
    transcript_confidence double precision,
    raw                   jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_messages_provider_msgid UNIQUE (provider, provider_message_id),
    CONSTRAINT ck_messages_direction CHECK (direction IN ('in','out')),
    CONSTRAINT ck_messages_kind CHECK (kind IN (
        'text','audio','image','video','document','interactive','location',
        'sticker','system','unsupported'))
);
CREATE INDEX ix_messages_account_id ON messages(account_id);
CREATE INDEX ix_messages_session_id ON messages(session_id);
CREATE INDEX ix_messages_account_created ON messages(account_id, created_at);

CREATE TABLE briefs (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id        uuid NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    brand_id          uuid NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    source_message_id uuid REFERENCES messages(id) ON DELETE SET NULL,
    parent_brief_id   uuid REFERENCES briefs(id) ON DELETE SET NULL,
    version           integer NOT NULL DEFAULT 1,
    status            varchar(24) NOT NULL DEFAULT 'draft',
    payload           jsonb NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_briefs_status CHECK (
        status IN ('draft','approved','rejected','superseded'))
);
CREATE INDEX ix_briefs_account_id ON briefs(account_id);
CREATE INDEX ix_briefs_brand_id ON briefs(brand_id);

CREATE TABLE creatives (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    brief_id          uuid NOT NULL REFERENCES briefs(id) ON DELETE CASCADE,
    brand_id          uuid NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    template          varchar(64) NOT NULL,
    aspect            varchar(16) NOT NULL DEFAULT '1:1',
    width             integer NOT NULL DEFAULT 1080,
    height            integer NOT NULL DEFAULT 1080,
    background_key    text,
    background_url    text,
    composed_key      text,
    composed_url      text,
    imagegen_provider varchar(32),
    imagegen_job_id   varchar(120),
    cost_micros       bigint NOT NULL DEFAULT 0,
    status            varchar(24) NOT NULL DEFAULT 'pending',
    error             text,
    timings           jsonb NOT NULL DEFAULT '{}'::jsonb,
    expires_at        timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_creatives_status CHECK (status IN (
        'pending','generating','composing','ready','failed','approved','published','expired'))
);
CREATE INDEX ix_creatives_brief_id ON creatives(brief_id);
CREATE INDEX ix_creatives_brand_id ON creatives(brand_id);

CREATE TABLE publications (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    creative_id       uuid NOT NULL REFERENCES creatives(id) ON DELETE CASCADE,
    ig_account_id     uuid REFERENCES ig_accounts(id) ON DELETE SET NULL,
    media_type        varchar(16) NOT NULL DEFAULT 'IMAGE',
    caption           text,
    hashtags          text[] NOT NULL DEFAULT '{}',
    ig_container_id   varchar(64),
    ig_media_id       varchar(64),
    permalink         text,
    status            varchar(24) NOT NULL DEFAULT 'queued',
    scheduled_for     timestamptz,
    published_at      timestamptz,
    error             text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_publications_status CHECK (status IN (
        'queued','scheduled','creating_container','publishing','published','failed','cancelled'))
);
CREATE INDEX ix_publications_creative_id ON publications(creative_id);

-- The only vector column in the database.
CREATE TABLE brand_memory (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id   uuid NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    kind       varchar(32) NOT NULL,
    content    text NOT NULL,
    embedding  vector(:EMBED_DIM),
    meta       jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_ref text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_brand_memory_kind CHECK (kind IN (
        'past_creative','feedback','product','campaign','fact','note'))
);
CREATE INDEX ix_brand_memory_brand_id ON brand_memory(brand_id);

CREATE TABLE credit_ledger (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      uuid NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    delta           integer NOT NULL,
    balance_after   integer NOT NULL,
    reason          varchar(48) NOT NULL,
    ref_type        varchar(32),
    ref_id          varchar(64),
    idempotency_key varchar(120) UNIQUE,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_credit_ledger_account_id ON credit_ledger(account_id);

CREATE TABLE jobs (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind          varchar(48) NOT NULL,
    dedupe_key    varchar(160) UNIQUE,
    payload       jsonb NOT NULL DEFAULT '{}'::jsonb,
    status        varchar(16) NOT NULL DEFAULT 'queued',
    attempts      integer NOT NULL DEFAULT 0,
    last_error    text,
    scheduled_for timestamptz,
    started_at    timestamptz,
    finished_at   timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_jobs_status CHECK (status IN ('queued','running','done','failed','dead'))
);
CREATE INDEX ix_jobs_kind ON jobs(kind);

CREATE TABLE stage_timings (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id   varchar(64) NOT NULL,
    account_id uuid,
    stage      varchar(48) NOT NULL,
    ms         integer NOT NULL,
    ok         boolean NOT NULL DEFAULT true,
    meta       jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_stage_timings_trace_id ON stage_timings(trace_id);
"""

# Deliberately NOT created here. On an empty table a sequential scan beats HNSW,
# and building the index before you have rows just costs you build time twice.
# Run this once brand_memory has a few thousand rows:
#
#   CREATE INDEX CONCURRENTLY ix_brand_memory_embedding
#       ON brand_memory USING hnsw (embedding vector_cosine_ops)
#       WITH (m = 16, ef_construction = 64);
HNSW_INDEX = """
CREATE INDEX IF NOT EXISTS ix_brand_memory_embedding
    ON brand_memory USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
"""

DROP = """
DROP TABLE IF EXISTS stage_timings, jobs, credit_ledger, brand_memory, publications,
    creatives, briefs, messages, wa_sessions, ig_accounts, brands, accounts CASCADE;
"""


def upgrade() -> None:
    op.execute(DDL.replace(":EMBED_DIM", str(settings.embed_dim)))


def downgrade() -> None:
    op.execute(DROP)
