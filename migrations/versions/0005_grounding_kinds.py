"""grounding lanes: new brand_memory kinds and a composite index

Revision ID: 0005
Revises: 0004

The brief contract has always had a `grounding` object with three lists --
catalog_item_ids, style_anchor_ids, rejection_ids -- and nothing populated
them. This is the schema half of making them real.

Two kinds are added:

* `product`   already existed; it becomes the catalogue lane.
* `style_anchor` -- a creative that worked, kept deliberately rather than
  inferred from "the most recent one".
* `rejection` -- something the client turned down, and why. This is the lane
  that earns its keep: a brand's "no" is the hardest thing for a model to
  guess and the most expensive thing to get wrong twice.

Retrieval filters by kind before ranking, so the useful index is
(brand_id, kind), not brand_id on its own.
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

UP = """
ALTER TABLE brand_memory DROP CONSTRAINT ck_brand_memory_kind;
ALTER TABLE brand_memory ADD CONSTRAINT ck_brand_memory_kind CHECK (kind IN (
    'product','style_anchor','rejection','past_creative','feedback',
    'campaign','fact','note'));
CREATE INDEX ix_brand_memory_brand_kind ON brand_memory(brand_id, kind);
"""

DOWN = """
DROP INDEX IF EXISTS ix_brand_memory_brand_kind;
DELETE FROM brand_memory WHERE kind IN ('style_anchor','rejection');
ALTER TABLE brand_memory DROP CONSTRAINT ck_brand_memory_kind;
ALTER TABLE brand_memory ADD CONSTRAINT ck_brand_memory_kind CHECK (kind IN (
    'past_creative','feedback','product','campaign','fact','note'));
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
