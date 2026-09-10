"""SQLAlchemy models. Mirrors migrations/versions/0001_initial.py exactly.

Two deliberate schema decisions, both load-bearing:

1. `brand_memory.embedding` is the ONLY vector column in the database. Brand
   identity is not embedded -- it lives in plain columns on `brands` and is
   loaded whole into every prompt, so a `never_say` rule can never be missed
   because it ranked below a similarity threshold.
2. `briefs.payload` holds the brief contract, whose `visual_direction.prompt`
   describes background imagery only. Headline / CTA / logo are composited
   afterwards with the brand's real fonts, which is what makes a revision
   nearly free: edit the brief, re-composite, skip the image call.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import settings


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


TS = DateTime(timezone=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TS, server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- #
# tenancy
# --------------------------------------------------------------------------- #
class Account(Base, TimestampMixin):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = _pk()
    wa_phone: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(120))
    locale: Mapped[str] = mapped_column(String(16), default="en-IN", nullable=False)
    # The script they TYPE in (latin / devanagari / ...). Locked only from typed
    # messages: a transcript's script is the vendor's, not the owner's.
    script: Mapped[str | None] = mapped_column(String(16))
    plan: Mapped[str] = mapped_column(String(32), default="trial", nullable=False)
    credits_balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    onboarded_at: Mapped[datetime | None] = mapped_column(TS)
    blocked_at: Mapped[datetime | None] = mapped_column(TS)
    # If their very first message already described a real creative, keep it
    # here through onboarding and generate it the moment setup finishes, so
    # they never repeat themselves. Cleared once used.
    pending_first_request: Mapped[str | None] = mapped_column(Text)

    brands: Mapped[list[Brand]] = relationship(back_populates="account")

    __table_args__ = (CheckConstraint("credits_balance >= 0", name="ck_accounts_credits_nonneg"),)


class Brand(Base, TimestampMixin):
    """Brand identity. Every column here is loaded whole into the system prompt."""

    __tablename__ = "brands"

    id: Mapped[uuid.UUID] = _pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    category: Mapped[str | None] = mapped_column(String(80))
    tagline: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    target_audience: Mapped[str | None] = mapped_column(Text)
    tone: Mapped[str | None] = mapped_column(Text)
    languages: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    never_say: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    always_say: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    cta_defaults: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    palette: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    fonts: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    logo_url: Mapped[str | None] = mapped_column(Text)
    watermark_url: Mapped[str | None] = mapped_column(Text)
    template_prefs: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Filled from the client's actual logo pixels, not from what they say.
    logo_notes: Mapped[str | None] = mapped_column(Text)
    logo_analysis: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # `category` is what the client said, in their words. `industry_slug` is the
    # normalised key research is cached under. The old schema keyed a shared
    # cache on free text, so "handmade gifting business" and "handmade gifts"
    # were two cache entries and it effectively never hit.
    industry_slug: Mapped[str | None] = mapped_column(String(60), index=True)

    account: Mapped[Account] = relationship(back_populates="brands")


class IgAccount(Base, TimestampMixin):
    __tablename__ = "ig_accounts"

    id: Mapped[uuid.UUID] = _pk()
    brand_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ig_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    username: Mapped[str | None] = mapped_column(String(120))
    access_token: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(TS)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="connected", nullable=False)

    __table_args__ = (UniqueConstraint("brand_id", "ig_user_id", name="uq_ig_brand_user"),)


# --------------------------------------------------------------------------- #
# conversation
# --------------------------------------------------------------------------- #
class WaSession(Base, TimestampMixin):
    """One row per WhatsApp conversation window (Meta's 24h customer-service window)."""

    __tablename__ = "wa_sessions"

    id: Mapped[uuid.UUID] = _pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    wa_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    last_inbound_at: Mapped[datetime | None] = mapped_column(TS)
    last_outbound_at: Mapped[datetime | None] = mapped_column(TS)
    window_expires_at: Mapped[datetime | None] = mapped_column(TS)
    active_brief_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    state: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(TS)

    # At most one live window per (account, wa_id). Enforced in the database
    # because two webhooks can land in the same millisecond.
    __table_args__ = (
        Index(
            "uq_wa_sessions_live",
            "account_id",
            "wa_id",
            unique=True,
            postgresql_where=text("closed_at IS NULL"),
        ),
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = _pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("wa_sessions.id", ondelete="SET NULL"), index=True
    )
    channel: Mapped[str] = mapped_column(String(24), default="whatsapp", nullable=False)
    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(160))
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    media_url: Mapped[str | None] = mapped_column(Text)
    media_mime: Mapped[str | None] = mapped_column(String(80))
    media_duration_ms: Mapped[int | None] = mapped_column(Integer)
    transcript: Mapped[str | None] = mapped_column(Text)
    transcript_provider: Mapped[str | None] = mapped_column(String(32))
    transcript_lang: Mapped[str | None] = mapped_column(String(16))
    transcript_confidence: Mapped[float | None] = mapped_column()
    # Stamped by the agent turn that folded this inbound message into its
    # reply. NULL means no turn has answered it yet.
    answered_at: Mapped[datetime | None] = mapped_column(TS)
    raw: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("provider", "provider_message_id", name="uq_messages_provider_msgid"),
        CheckConstraint("direction in ('in','out')", name="ck_messages_direction"),
        CheckConstraint(
            "kind in ('text','audio','image','video','document','interactive','location',"
            "'sticker','system','unsupported')",
            name="ck_messages_kind",
        ),
        Index("ix_messages_account_created", "account_id", "created_at"),
    )


# --------------------------------------------------------------------------- #
# creative pipeline
# --------------------------------------------------------------------------- #
class Brief(Base):
    __tablename__ = "briefs"

    id: Mapped[uuid.UUID] = _pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    brand_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    parent_brief_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("briefs.id", ondelete="SET NULL")
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status in ('draft','approved','rejected','superseded')", name="ck_briefs_status"
        ),
    )


class Creative(Base):
    __tablename__ = "creatives"

    id: Mapped[uuid.UUID] = _pk()
    brief_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("briefs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    brand_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True
    )
    template: Mapped[str] = mapped_column(String(64), nullable=False)
    aspect: Mapped[str] = mapped_column(String(16), default="1:1", nullable=False)
    # A carousel is N creatives sharing one group id; slide_position is 1-based.
    carousel_group_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    slide_position: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    width: Mapped[int] = mapped_column(Integer, default=1080, nullable=False)
    height: Mapped[int] = mapped_column(Integer, default=1080, nullable=False)
    background_key: Mapped[str | None] = mapped_column(Text)
    background_url: Mapped[str | None] = mapped_column(Text)
    composed_key: Mapped[str | None] = mapped_column(Text)
    composed_url: Mapped[str | None] = mapped_column(Text)
    imagegen_provider: Mapped[str | None] = mapped_column(String(32))
    imagegen_job_id: Mapped[str | None] = mapped_column(String(120))
    # Was THIS slide charged for? Set at charge time so a reaper that finds the
    # row stuck after a crash can refund exactly what was paid, once.
    billed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cost_micros: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    quality_score: Mapped[int | None] = mapped_column(Integer)
    timings: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(TS)
    # Set only by the client's own tap/reply. Publishing checks this column,
    # not the agent's opinion of whether they agreed.
    approved_at: Mapped[datetime | None] = mapped_column(TS)
    approved_via: Mapped[str | None] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status in ('pending','generating','composing','ready','failed','approved',"
            "'published','expired')",
            name="ck_creatives_status",
        ),
        Index(
            "ix_creatives_approved_at",
            "approved_at",
            postgresql_where=text("approved_at IS NOT NULL"),
        ),
    )


class Publication(Base):
    __tablename__ = "publications"

    id: Mapped[uuid.UUID] = _pk()
    creative_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("creatives.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ig_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ig_accounts.id", ondelete="SET NULL")
    )
    media_type: Mapped[str] = mapped_column(String(16), default="IMAGE", nullable=False)
    caption: Mapped[str | None] = mapped_column(Text)
    hashtags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    ig_container_id: Mapped[str | None] = mapped_column(String(64))
    child_container_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, nullable=False
    )
    ig_media_id: Mapped[str | None] = mapped_column(String(64))
    alt_text: Mapped[str | None] = mapped_column(Text)
    permalink: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False)
    scheduled_for: Mapped[datetime | None] = mapped_column(TS)
    published_at: Mapped[datetime | None] = mapped_column(TS)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status in ('queued','scheduled','creating_container','publishing','published',"
            "'failed','cancelled')",
            name="ck_publications_status",
        ),
    )


class BrandAsset(Base):
    """Real photographs the owner sent: product shots, the shop, the team.

    A brief references one by id in `visual_direction.reference_asset_id` when the
    post should feature the actual product rather than a generated stand-in.
    """

    __tablename__ = "brand_assets"

    id: Mapped[uuid.UUID] = _pk()
    brand_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(24), default="product", nullable=False)
    label: Mapped[str | None] = mapped_column(String(160))
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    mime: Mapped[str | None] = mapped_column(String(80))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "kind in ('product','logo','shop','team','other')", name="ck_brand_assets_kind"
        ),
    )


# --------------------------------------------------------------------------- #
# memory -- the only vector column in the schema
# --------------------------------------------------------------------------- #
class BrandMemory(Base):
    __tablename__ = "brand_memory"

    id: Mapped[uuid.UUID] = _pk()
    brand_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embed_dim))
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    source_ref: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "kind in ('product','style_anchor','rejection','past_creative',"
            "'feedback','campaign','fact','note')",
            name="ck_brand_memory_kind",
        ),
        # Retrieval always filters by kind before ranking, so the index that
        # matters is the composite, not brand_id alone.
        Index("ix_brand_memory_brand_kind", "brand_id", "kind"),
    )


# --------------------------------------------------------------------------- #
# ops
# --------------------------------------------------------------------------- #
class IndustryResearch(Base):
    """Shared per-industry style research, cached across every brand in it.

    Keyed on a normalised slug, not the client's own words. Free text as a
    primary key means a cache that is written on every miss and read on none.
    """

    __tablename__ = "industry_research"

    industry_slug: Mapped[str] = mapped_column(String(60), primary_key=True)
    label: Mapped[str | None] = mapped_column(String(120))
    style_summary: Mapped[str] = mapped_column(Text, nullable=False)
    sources: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    refreshed_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)


class LearningEvent(Base):
    """Did we record a usable signal from this creative, or skip it, and why.

    Separate from brand_memory: memory holds what was learned, this holds
    whether learning happened at all. Without it you cannot tell a brand whose
    creatives keep getting rejected from one nobody has looked at.
    """

    __tablename__ = "learning_events"

    id: Mapped[uuid.UUID] = _pk()
    brand_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True
    )
    creative_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("creatives.id", ondelete="SET NULL")
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    quality_score: Mapped[int | None] = mapped_column(Integer)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "event_type in ('recorded','skipped_quality','skipped_no_profile',"
            "'skipped_free_revision','rejected_by_client','distilled')",
            name="ck_learning_events_type",
        ),
    )


class AnalyticsEvent(Base):
    """Activation funnel. Answers "where do clients stop", which is the only
    question that matters before there is revenue to analyse."""

    __tablename__ = "analytics_events"

    id: Mapped[uuid.UUID] = _pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    event_metadata: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "event_type in ('signup','onboarding_completed','logo_received',"
            "'first_creative_shown','first_creative_approved','first_published',"
            "'returned_voluntarily','topup','churn_risk')",
            name="ck_analytics_events_type",
        ),
    )


class AdAccount(Base):
    """The client's OWN Meta ad account. Phase 2, but modelled now because
    partner access has states, and states belong in rows, not in a column on
    a table about something else."""

    __tablename__ = "ad_accounts"

    id: Mapped[uuid.UUID] = _pk()
    brand_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True
    )
    meta_ad_account_id: Mapped[str | None] = mapped_column(String(64))
    meta_business_id: Mapped[str | None] = mapped_column(String(64))
    partner_access_status: Mapped[str] = mapped_column(
        String(24), default="not_connected", nullable=False
    )
    verified_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TS, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "partner_access_status in ('not_connected','pending_approval','granted','revoked')",
            name="ck_ad_accounts_status",
        ),
    )


class CreditLedger(Base):
    __tablename__ = "credit_ledger"

    id: Mapped[uuid.UUID] = _pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(48), nullable=False)
    ref_type: Mapped[str | None] = mapped_column(String(32))
    ref_id: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(120), unique=True)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)


class CreativeEvent(Base):
    """One vote: what the owner did with a creative, and what it was at the time."""

    __tablename__ = "creative_events"

    id: Mapped[uuid.UUID] = _pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    brand_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True
    )
    brief_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    creative_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "kind in ('created','approve','change_words','change_picture','revise',"
            "'regenerate','publish','suggested','suggestion_taken')",
            name="ck_creative_events_kind",
        ),
    )


class ContentPlan(Base):
    """A month of posts for one brand: goal, pillar mix, cadence, and the slots."""

    __tablename__ = "content_plans"

    id: Mapped[uuid.UUID] = _pk()
    brand_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True
    )
    month: Mapped[date] = mapped_column(Date, nullable=False)
    goal: Mapped[str] = mapped_column(String(24), default="awareness", nullable=False)
    cadence: Mapped[int] = mapped_column(SmallInteger, default=4, nullable=False)
    pillar_mix: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    slots: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TS, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "goal in ('footfall','leads','launch','awareness')", name="ck_content_plans_goal"
        ),
        CheckConstraint("cadence between 1 and 7", name="ck_content_plans_cadence"),
        UniqueConstraint("brand_id", "month", name="uq_content_plans_brand_month"),
    )


class Job(Base, TimestampMixin):
    """Durable mirror of the Upstash queue: dedupe, retries, and an audit trail."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = _pk()
    kind: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(160), unique=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    scheduled_for: Mapped[datetime | None] = mapped_column(TS)
    started_at: Mapped[datetime | None] = mapped_column(TS)
    finished_at: Mapped[datetime | None] = mapped_column(TS)

    __table_args__ = (
        CheckConstraint(
            "status in ('queued','running','done','failed','dead')", name="ck_jobs_status"
        ),
    )


class StageTiming(Base):
    """Per-stage latency. The thing you will stare at when a turn takes 40 seconds."""

    __tablename__ = "stage_timings"

    id: Mapped[uuid.UUID] = _pk()
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    stage: Mapped[str] = mapped_column(String(48), nullable=False)
    ms: Mapped[int] = mapped_column(Integer, nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
