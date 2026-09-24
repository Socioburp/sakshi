"""The onboarding kit against a real database. Skips where there is none.

Some of this lives in SQL and nowhere else, so it cannot be pinned by the unit
tests in test_onboarding.py: the brand_assets kind CHECK has to accept
'reference' (migration 0015), _resolve_photos has to leave a reference creative
out of the candidates it even loads, and the photo-day checklist has to count
photographs rather than rows. The first two are the difference between a
client's first post and a client's first post with two headlines on it.
"""

from __future__ import annotations

import hashlib
import io
import uuid

import pytest
from PIL import Image

from app.creative import photoref


@pytest.fixture
def db_ready():
    from sqlalchemy import text as sql_text

    from app.db.session import engine

    try:
        with engine.connect() as conn:
            conn.execute(sql_text("select 1 from brand_assets limit 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no database with the schema available: {str(exc)[:80]}")


@pytest.fixture
def brand(db_ready):
    from app.db.models import Account, Brand
    from app.db.session import session_scope

    with session_scope() as db:
        acct = Account(wa_phone=f"test-{uuid.uuid4().hex[:12]}", credits_balance=10)
        db.add(acct)
        db.flush()
        row = Brand(
            account_id=acct.id,
            name="Onboarding Test",
            category="cold-pressed oils",
            palette={"primary": "#123B2E", "ink": "#FFFFFF"},
        )
        db.add(row)
        db.flush()
        return row.id


def _image(colour=(180, 150, 120)) -> bytes:
    im = Image.new("RGB", (1600, 2000), colour)
    px = im.load()
    for y in range(0, 2000, 3):
        for x in range(0, 1600, 3):
            px[x, y] = (colour[0] // 2, colour[1], colour[2] // 3)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=95)
    return out.getvalue()


def _asset(db, brand_id, kind, label, key):
    from app.db.models import BrandAsset

    row = BrandAsset(
        brand_id=brand_id,
        kind=kind,
        label=label,
        storage_key=key,
        url=f"https://cdn.test/{key}",
        mime="image/jpeg",
        width=1600,
        height=2000,
    )
    db.add(row)
    db.flush()
    return row


def test_a_reference_creative_can_be_stored_at_all(brand):
    """The CHECK constraint is the whole of migration 0015: without it the
    onboarding run dies halfway, having stored the product photos."""
    from app.db.models import BrandAsset
    from app.db.session import session_scope

    with session_scope() as db:
        row = _asset(db, brand, "reference", "a diwali post", f"onboarding/{brand}/reference/a.jpg")
        assert db.get(BrandAsset, row.id).kind == "reference"


def test_a_reference_creative_is_never_a_candidate_photo(brand):
    """The guarantee, at the level it is actually enforced: the query."""
    from app.creative.brief import EXAMPLE, CreativeBrief
    from app.creative.pipeline import _resolve_photos
    from app.db.session import session_scope

    # Words with meaning in them: photoref throws away "weekend" and "sale", so
    # the stock example headline matches nothing and would prove nothing.
    brief = CreativeBrief.model_validate({**EXAMPLE, "headline": "Kaju katli box, fresh today"})
    label = "kaju katli box"
    with session_scope() as db:
        ref = _asset(db, brand, "reference", label, f"onboarding/{brand}/reference/b.jpg")
        _asset(db, brand, "logo", "Logo", f"onboarding/{brand}/photo/logo.jpg")
        photo = _asset(db, brand, "product", label, f"onboarding/{brand}/photo/c.jpg")

        # The reference's label is the headline itself, so on words alone it
        # would outscore the product photo and take the slide.
        chosen = _resolve_photos(db, brand, brief, brief.units())
        assert set(chosen.values()) == {str(photo.id)}

        # An explicit reference to one is refused as well, not honoured: that is
        # the path an agent could otherwise take after list_brand_assets.
        units = brief.units()
        units[0].visual_direction.reference_asset_id = str(ref.id)
        assert str(ref.id) not in _resolve_photos(db, brand, brief, units).values()
        assert photoref.USABLE_KINDS.isdisjoint({"reference", "logo"})


def test_the_second_onboarding_run_stores_nothing_twice(brand, monkeypatch):
    """Staff re-run the command every time the team adds files. A second run that
    duplicated every asset would double the free photo lane's candidate list and
    make the same photograph win two slides of one carousel."""
    import importlib.util
    import sys
    from pathlib import Path

    from sqlalchemy import func, select

    from app.db.models import BrandAsset
    from app.db.session import session_scope

    spec = importlib.util.spec_from_file_location(
        "onboard_brand", Path(__file__).resolve().parents[1] / "scripts" / "onboard_brand.py"
    )
    onboard = importlib.util.module_from_spec(spec)
    sys.modules["onboard_brand"] = onboard
    spec.loader.exec_module(onboard)
    monkeypatch.setattr(onboard.r2, "put", lambda key, data, mime=None: f"https://cdn.test/{key}")

    files = [
        onboard.SourceFile("jar.jpg", _image()),
        onboard.SourceFile("shopfront.jpg", _image((90, 130, 150))),
    ]
    for _ in range(2):
        with session_scope() as db:
            known = onboard.stored_keys(db, brand)
            plan = onboard.plan_products(files, brand_id=str(brand), known_keys=known)
            for item in plan.stored:
                onboard.store(db, brand, item)

    with session_scope() as db:
        count = db.scalar(
            select(func.count())
            .select_from(BrandAsset)
            .where(BrandAsset.brand_id == brand, BrandAsset.kind != "logo")
        )
        assert count == 2
        keys = set(db.scalars(select(BrandAsset.storage_key).where(BrandAsset.brand_id == brand)))
        digest = hashlib.sha256(files[0].data).hexdigest()[:32]
        assert f"onboarding/{brand}/photo/{digest}.jpg" in keys


def test_the_database_refuses_the_same_onboarding_file_twice(brand):
    """The command reads what is stored, then spends minutes downloading a folder
    and running a vision pass over it, and writes after. Two staff onboarding the
    same brand at once both read an empty set and both insert. Nothing in the
    schema stopped them, so the free photo lane held two rows for the same jar --
    and _resolve_photos de-duplicates by asset id, not by content, so the same
    photograph could win two slides of one carousel."""
    from sqlalchemy.exc import IntegrityError

    from app.db.session import session_scope

    key = f"onboarding/{brand}/photo/deadbeef.jpg"
    with session_scope() as db:
        _asset(db, brand, "product", "a jar", key)

    with pytest.raises(IntegrityError):
        with session_scope() as db:
            _asset(db, brand, "product", "the same jar again", key)

    # Only the onboarding lane. WhatsApp keys carry the message id and are
    # unique already; making them unique in the schema would turn a re-delivered
    # message into a crash loop in the queue handler instead of a spare row.
    chat_key = f"published/2026/01/01/{brand}/{uuid.uuid4()}-product.jpg"
    with session_scope() as db:
        _asset(db, brand, "product", "from whatsapp", chat_key)
        _asset(db, brand, "product", "from whatsapp again", chat_key)


def test_a_brief_can_still_collect_more_than_one_memory(brand):
    """The onboarding uniqueness must not spread to 'brief:<id>'. One brief gets
    a 'feedback' row when the revision that produced it was made and a
    'style_anchor' row when the owner approves it -- and votes._remember swallows
    what it raises, so a blanket index would silently stop recording approvals."""
    from app.config import settings
    from app.db.session import session_scope
    from app.memory import embed

    if not settings.voyage_api_key:
        pytest.skip("no VOYAGE_API_KEY: a memory written without one is unretrievable")

    ref = f"brief:{uuid.uuid4()}"
    with session_scope() as db:
        embed.remember(
            db, brand_id=brand, kind="feedback", content="Owner asked: warmer", source_ref=ref
        )
        embed.remember(
            db, brand_id=brand, kind="style_anchor", content="Owner approved it", source_ref=ref
        )


def test_a_brand_seeded_with_references_alone_is_still_asked_for_photos(brand):
    """The founder's plan is that our team's reference set goes up first and the
    client's raw photos follow. The photo-day checklist counted everything that
    was not the logo, so eight 'reference' rows read as eight photos: the one
    mechanism that would have asked the owner for real ones switched itself off,
    the free photo lane could never fire, and every creative was billed."""
    from app.db.session import session_scope
    from app.queue.handlers import usable_photo_count

    with session_scope() as db:
        for i in range(8):
            _asset(db, brand, "reference", f"a post {i}", f"onboarding/{brand}/reference/r{i}.jpg")
        assert usable_photo_count(db, brand) == 0

        _asset(db, brand, "logo", "Logo", f"onboarding/{brand}/photo/logo.jpg")
        assert usable_photo_count(db, brand) == 0

        # Two real photographs, and the checklist is still owed: the bar is three.
        _asset(db, brand, "product", "a jar", f"onboarding/{brand}/photo/p1.jpg")
        _asset(db, brand, "shop", "the shopfront", f"onboarding/{brand}/photo/p2.jpg")
        assert usable_photo_count(db, brand) == 2


def test_a_style_anchor_is_written_once_per_reference(brand):
    """The memory is keyed on the file's own bytes, so a re-run adds nothing and
    the style lane is not buried under ten copies of the same anchor."""
    from sqlalchemy import select

    from app.config import settings
    from app.db.models import BrandMemory
    from app.db.session import session_scope
    from app.memory import embed

    if not settings.voyage_api_key:
        pytest.skip("no VOYAGE_API_KEY: a memory written without one is unretrievable")

    source_ref = f"onboarding:{hashlib.sha256(b'ref').hexdigest()[:16]}"
    for _ in range(2):
        with session_scope() as db:
            already = set(
                db.scalars(
                    select(BrandMemory.source_ref).where(BrandMemory.brand_id == brand)
                ).all()
            )
            if source_ref in already:
                continue
            embed.remember(
                db,
                brand_id=brand,
                kind="style_anchor",
                content="Onboarding Test reference creative: a dark jar on a cream panel.",
                meta={"source": "onboarding_reference", "layout": "lower_third"},
                source_ref=source_ref,
            )

    with session_scope() as db:
        rows = list(
            db.scalars(
                select(BrandMemory).where(
                    BrandMemory.brand_id == brand, BrandMemory.source_ref == source_ref
                )
            )
        )
        assert len(rows) == 1
        assert rows[0].meta["layout"] == "lower_third"


# --------------------------------------------------------------------------- #
# naming a brand by the number the client messages us from
# --------------------------------------------------------------------------- #
def _account_with_brands(names, phone):
    from app.db.models import Account, Brand
    from app.db.session import session_scope

    with session_scope() as db:
        acct = Account(wa_phone=phone, credits_balance=10)
        db.add(acct)
        db.flush()
        made = []
        for i, name in enumerate(names):
            row = Brand(account_id=acct.id, name=name, is_default=(i == 0))
            db.add(row)
            db.flush()
            made.append(row.id)
        return made


def test_the_number_the_client_messages_us_from_finds_their_brand(db_ready):
    """Nobody on the team knows a brand by its uuid. They know the client by
    their number, which IS the account: the webhook creates one keyed by it on
    first contact, so the kit can be named the way the team thinks."""
    from app.db.session import session_scope
    from scripts import onboard_brand as onboard

    digits = f"9199{uuid.uuid4().int % 10**8:08d}"
    (made,) = _account_with_brands(["Anaya Foods"], digits)

    with session_scope() as db:
        found, why = onboard.brand_for_phone(db, digits)

    assert found == made and why == ""


def test_the_number_is_found_however_a_person_writes_it(db_ready):
    """A person types +91 98765 43210 and Meta sends 919876543210. A kit
    refused over a space is a kit loaded late."""
    from app.db.session import session_scope
    from scripts import onboard_brand as onboard

    digits = f"9198{uuid.uuid4().int % 10**8:08d}"
    (made,) = _account_with_brands(["Kadamba Sweets"], digits)
    typed = f"+{digits[:2]} {digits[2:7]} {digits[7:]}"

    with session_scope() as db:
        found, why = onboard.brand_for_phone(db, typed)

    assert found == made, why


def test_a_number_that_has_never_messaged_us_says_why_not(db_ready):
    """The kit can only be loaded after the client has said hello, because
    there is no account before that. Say that, rather than 'not found'."""
    from app.db.session import session_scope
    from scripts import onboard_brand as onboard

    with session_scope() as db:
        found, why = onboard.brand_for_phone(db, "919000000000")

    assert found is None
    assert "message" in why.lower(), why


def test_a_number_with_two_brands_and_no_default_asks_which(db_ready):
    """Guessing here would seed the wrong brand with another brand's look,
    and nothing downstream would notice. Name them and stop."""
    from app.db.models import Brand
    from app.db.session import session_scope
    from scripts import onboard_brand as onboard

    digits = f"9197{uuid.uuid4().int % 10**8:08d}"
    made = _account_with_brands(["Anaya Foods", "Anaya Cafe"], digits)
    with session_scope() as db:
        db.get(Brand, made[0]).is_default = False

    with session_scope() as db:
        found, why = onboard.brand_for_phone(db, digits)

    assert found is None
    assert all(str(b) in why for b in made), "name both, so the operator can choose"
    assert "--brand" in why
