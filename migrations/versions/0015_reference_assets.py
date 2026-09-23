"""brand_assets kind 'reference': the creatives our own team made for this brand

Revision ID: 0015
Revises: 0014

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
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

UP = """
ALTER TABLE brand_assets DROP CONSTRAINT ck_brand_assets_kind;
ALTER TABLE brand_assets ADD CONSTRAINT ck_brand_assets_kind CHECK (kind IN (
    'product','logo','shop','team','other','reference'));
"""

DOWN = """
DELETE FROM brand_assets WHERE kind = 'reference';
ALTER TABLE brand_assets DROP CONSTRAINT ck_brand_assets_kind;
ALTER TABLE brand_assets ADD CONSTRAINT ck_brand_assets_kind CHECK (kind IN (
    'product','logo','shop','team','other'));
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
