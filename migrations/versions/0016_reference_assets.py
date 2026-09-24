"""the onboarding kit in SQL: kind 'reference', and one row per file

Revision ID: 0016
Revises: 0015

When a brand is onboarded, SocioBurp's designers hand over five to ten
finished creatives as the example of what this brand should look like. Those
files are kept, because a human onboarding a second brand for the same client
needs to see what seeded the first one -- but they are NOT photographs of the
product. A reference creative carries its own headline and its own logo, so
compositing over one would put two headlines and two marks on the same post,
and handing one to the image model would make it copy the lettering.

They therefore get a kind of their own rather than being filed as 'other':
every lane that picks a picture to build on works from a whitelist of kinds
(app/creative/photoref.USABLE_KINDS) and a new kind is invisible to all of
them by construction.

The two unique indexes put the onboarding command's idempotency where its own
comment already claims it lives. Every onboarding file is named by the hash of
its own bytes ("the content hash IS the identity", r2.onboarding_key) and every
style anchor by the same digest, but the command reads what is already stored
BEFORE it downloads a folder and runs a vision pass over it, and writes minutes
later. Two staff onboarding the same brand at once -- one handover, one person
adding files and re-running -- both read an empty set and both insert. The free
photo lane would then hold two rows for the same jar, and _resolve_photos
de-duplicates by asset id rather than by content, so the same photograph could
win two slides of one carousel: precisely what its own comment says must not
happen. The style lane would retrieve the anchor twice and count it twice.

Both are deliberately partial, scoped to the onboarding lane by its key prefix,
and NOT blanket uniqueness on the tables:

* brand_assets keys from WhatsApp carry the message id, so they are unique
  already; a blanket index would turn a re-delivered message into an
  IntegrityError inside the queue handler, which retries -- a crash loop in
  place of a duplicate row.
* brand_memory source_refs of the form 'brief:<id>' are legitimately not
  unique. One brief collects a 'feedback' row when the revision that produced
  it was made and a 'style_anchor' row when the owner approves it, and
  votes._remember swallows what it raises -- so a blanket index would silently
  stop recording approvals, which is the failure embed.py refuses to allow.
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

UP = """
ALTER TABLE brand_assets DROP CONSTRAINT ck_brand_assets_kind;
ALTER TABLE brand_assets ADD CONSTRAINT ck_brand_assets_kind CHECK (kind IN (
    'product','logo','shop','team','other','reference'));

CREATE UNIQUE INDEX uq_brand_assets_onboarding_key
    ON brand_assets (brand_id, storage_key)
    WHERE storage_key LIKE 'onboarding/%';

CREATE UNIQUE INDEX uq_brand_memory_onboarding_ref
    ON brand_memory (brand_id, source_ref)
    WHERE source_ref LIKE 'onboarding:%';
"""

DOWN = """
DROP INDEX IF EXISTS uq_brand_memory_onboarding_ref;
DROP INDEX IF EXISTS uq_brand_assets_onboarding_key;
DELETE FROM brand_assets WHERE kind = 'reference';
ALTER TABLE brand_assets DROP CONSTRAINT ck_brand_assets_kind;
ALTER TABLE brand_assets ADD CONSTRAINT ck_brand_assets_kind CHECK (kind IN (
    'product','logo','shop','team','other'));
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
