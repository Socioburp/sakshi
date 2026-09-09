"""carousel, brand assets, and Instagram accessibility fields

Revision ID: 0002
Revises: 0001

Three additions, all driven by the canonical brief contract in
docs/brief_schema.json:

* `brand_assets` -- what `visual_direction.reference_asset_id` points at, so a
  post can feature the owner's real product photo instead of a generated
  stand-in.
* carousel columns on `creatives` -- a carousel is N creatives sharing one
  `carousel_group_id`, ordered by `slide_position`. Modelling it as rows rather
  than a blob means one slide can fail, be retried, or be revised on its own.
* `publications.child_container_ids` -- Instagram's carousel publish is a
  two-level container dance: one container per slide, then a parent CAROUSEL
  container holding their ids. Losing those ids mid-publish means orphaned
  containers you cannot clean up, so they are persisted before the parent call.
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

UP = """
CREATE TABLE brand_assets (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id          uuid NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    kind              varchar(24) NOT NULL DEFAULT 'product',
    label             varchar(160),
    storage_key       text NOT NULL,
    url               text,
    mime              varchar(80),
    width             integer,
    height            integer,
    source_message_id uuid,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_brand_assets_kind CHECK (kind IN ('product','logo','shop','team','other'))
);
CREATE INDEX ix_brand_assets_brand_id ON brand_assets(brand_id);

ALTER TABLE creatives ADD COLUMN carousel_group_id uuid;
ALTER TABLE creatives ADD COLUMN slide_position integer NOT NULL DEFAULT 1;
CREATE INDEX ix_creatives_carousel_group_id ON creatives(carousel_group_id);

ALTER TABLE publications ADD COLUMN child_container_ids text[] NOT NULL DEFAULT '{}';
ALTER TABLE publications ADD COLUMN alt_text text;
"""

DOWN = """
ALTER TABLE publications DROP COLUMN IF EXISTS alt_text;
ALTER TABLE publications DROP COLUMN IF EXISTS child_container_ids;
DROP INDEX IF EXISTS ix_creatives_carousel_group_id;
ALTER TABLE creatives DROP COLUMN IF EXISTS slide_position;
ALTER TABLE creatives DROP COLUMN IF EXISTS carousel_group_id;
DROP TABLE IF EXISTS brand_assets;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
