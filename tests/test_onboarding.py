"""The onboarding kit: a brand starts from our own team's work, not from nothing.

A new paying client has no votes and no approved posts, so every learned lane
in the product is silent for their first creatives -- exactly when "perfect
first time" is what they are paying for. The kit fixes that by hand: staff
upload the reference creatives our designers made and the raw product photos,
and this is what those two folders are allowed to turn into.

The tests are named after the failure each one pins. The loudest of them is
the reference creative: it is a finished post, with its own headline and its
own logo, and the day one of those is composited over or handed to the image
model the client gets a creative with two headlines on it.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import sys
import types
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from PIL import Image

from app.creative import brandkit, compose, photoref, refstyle, shotplan

# The staff command is a script, not a package: it is run by a person, from the
# repo, with the venv on the path, and never imported by the app.
_spec = importlib.util.spec_from_file_location(
    "onboard_brand", Path(__file__).resolve().parents[1] / "scripts" / "onboard_brand.py"
)
onboard = importlib.util.module_from_spec(_spec)
# Registered before it is executed: @dataclass looks its own module up by name
# while the class body is being processed, and a module loaded by path alone is
# not there yet.
sys.modules["onboard_brand"] = onboard
_spec.loader.exec_module(onboard)


@dataclass
class Asset:
    id: str
    kind: str = "product"
    label: str | None = None
    width: int | None = 1600
    height: int | None = 1600


def _image(size=(1600, 2000), colour=(180, 150, 120)) -> bytes:
    """A fixture with real texture in it: a flat fill reads as soft to the
    quality pass, which would make every fixture a rejected photo."""
    im = Image.new("RGB", size, colour)
    px = im.load()
    for y in range(0, size[1], 3):
        for x in range(0, size[0], 3):
            px[x, y] = (colour[0] // 2, colour[1], colour[2] // 3)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=95)
    return out.getvalue()


# --------------------------------------------------------------------------- #
# a reference creative is never a picture to build on
# --------------------------------------------------------------------------- #
def test_a_reference_creative_is_never_usable_as_a_photograph():
    """It already carries a headline and a logo: compositing over it ships two."""
    ref = Asset(id="ref", kind="reference", label="diwali mithai box post")
    assert not photoref.is_usable(ref)
    assert "reference" not in photoref.USABLE_KINDS


def test_a_reference_creative_never_wins_a_slide_however_well_it_matches():
    """The label of a reference names the product, so scoring alone would pick it."""
    ref = Asset(id="ref", kind="reference", label="kaju katli mithai box diwali")
    photo = Asset(id="photo", kind="product", label="kaju katli box")
    copy = "Kaju katli mithai box this Diwali"
    assert photoref.score(copy, "", ref) > photoref.score(copy, "", photo)
    assert photoref.choose(copy, [ref]) is None
    assert photoref.choose(copy, [ref, photo]) == "photo"


def test_the_photo_query_excludes_reference_assets():
    """_resolve_photos must not even load one: an explicit id cannot then slip through."""
    import uuid

    from app.creative.pipeline import _resolve_photos

    seen: dict[str, object] = {}

    class _Scalars:
        def __init__(self, stmt):
            seen["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": True}))

        def __iter__(self):
            return iter(())

    class _Db:
        def scalars(self, stmt):
            return _Scalars(stmt)

    brief = type("B", (), {"headline": "x"})()
    assert _resolve_photos(_Db(), uuid.uuid4(), brief, []) == {}
    sql = seen["sql"]
    assert "'logo'" in sql and "'reference'" in sql and "NOT IN" in sql.upper()


# --------------------------------------------------------------------------- #
# the style pass: strict, or there is no style
# --------------------------------------------------------------------------- #
GOOD = {
    "layout": "lower_third",
    "type_place": "solid_panel",
    "palette": ["#1F5B3D", "#F3E7D3"],
    "mood": ["calm", "premium"],
    "product": "whole",
    "light": "moody",
    "summary": "A dark bottle low in the frame with the words on a cream panel.",
}


def _answer(**over) -> str:
    return "Here you go:\n```json\n" + json.dumps({**GOOD, **over}) + "\n```"


def test_every_layout_family_the_style_pass_may_name_is_a_real_template():
    """A seeded family naming a template the compositor does not have is a refused
    render on the client's first creative."""
    assert set(refstyle.LAYOUTS) == set(compose.TEMPLATES)
    assert all(light in shotplan.SHOOT_STYLES for light in refstyle.LIGHTS)


def test_a_good_answer_parses_even_wrapped_in_prose():
    ref = refstyle.parse(_answer())
    assert ref.layout == "lower_third" and ref.light == "moody"
    assert ref.palette == ["#1F5B3D", "#F3E7D3"]


def test_a_missing_key_is_refused_not_defaulted():
    for key in GOOD:
        short = {k: v for k, v in GOOD.items() if k != key}
        with pytest.raises(refstyle.ReferenceUnreadable):
            refstyle.parse(json.dumps(short))


def test_a_layout_family_we_cannot_render_is_refused():
    with pytest.raises(refstyle.ReferenceUnreadable):
        refstyle.parse(_answer(layout="magazine_spread"))
    with pytest.raises(refstyle.ReferenceUnreadable):
        refstyle.parse(_answer(light="golden_hour"))


def test_a_colour_that_is_not_a_hex_is_refused():
    for bad in (["forest green"], ["#1F5B3"], ["#GGGGGG"], []):
        with pytest.raises(refstyle.ReferenceUnreadable):
            refstyle.parse(_answer(palette=bad))


def test_no_json_at_all_is_refused():
    with pytest.raises(refstyle.ReferenceUnreadable):
        refstyle.parse("I cannot describe this image.")
    with pytest.raises(refstyle.ReferenceUnreadable):
        refstyle.parse("{layout: lower_third,}")


async def test_the_style_pass_refuses_rather_than_guesses_with_no_model(monkeypatch):
    monkeypatch.setattr(refstyle.settings, "anthropic_model", "")
    with pytest.raises(refstyle.ReferenceUnreadable):
        await refstyle.describe(_image())


# --------------------------------------------------------------------------- #
# the aggregate: one reference set -> one brand kit
# --------------------------------------------------------------------------- #
def _ref(layout="lower_third", light="moody", place="solid_panel", product="whole", palette=None):
    return refstyle.Reference(
        layout=layout,
        type_place=place,
        palette=palette or ["#1F5B3D", "#F3E7D3"],
        mood=["calm"],
        product=product,
        light=light,
        summary="a post",
    )


def test_the_aggregate_takes_the_dominant_layout_light_and_palette():
    kit = refstyle.aggregate(
        [
            _ref(),
            _ref(),
            _ref(layout="frame_card", light="bright_airy"),
            _ref(palette=["#215E40", "#FFFFFF"]),
        ]
    )
    assert kit.layout == "lower_third" and kit.light == "moody"
    # #1F5B3D and #215E40 are the same green to everyone except ==.
    assert kit.palette[0] == "#1F5B3D"
    assert kit.counts["layout"] == {"lower_third": 3, "frame_card": 1}


def test_a_set_that_agrees_on_nothing_seeds_no_standing_rule():
    """Four posts that each do something different are four posts, not a house style."""
    kit = refstyle.aggregate(
        [
            _ref(layout="lower_third", product="whole"),
            _ref(layout="frame_card", product="detail"),
            _ref(layout="top_band", product="in_context"),
            _ref(layout="poster_stack", product="flat_lay"),
        ]
    )
    assert not any("built as" in lesson for lesson in kit.lessons)
    assert not any("product is shown" in lesson for lesson in kit.lessons)
    assert kit.layout in refstyle.LAYOUTS  # a kit is still decided, deterministically


def test_the_lessons_are_the_sentences_the_agent_will_read():
    kit = refstyle.aggregate([_ref(), _ref(), _ref()])
    assert "their posts are built as lower_third -- keep new ones in that family" in kit.lessons
    assert "their posts set the words on a solid panel" in kit.lessons
    assert "their product is shown whole" in kit.lessons
    assert len(kit.lessons) <= refstyle.MAX_LESSONS


def test_the_kit_is_the_same_whatever_order_the_folder_lists_files_in():
    refs = [_ref(), _ref(layout="frame_card"), _ref(layout="top_band"), _ref()]
    first = refstyle.aggregate(refs)
    assert refstyle.aggregate(list(reversed(refs))).lessons == first.lessons


# --------------------------------------------------------------------------- #
# the kit lands on the brand
# --------------------------------------------------------------------------- #
class _Brand:
    def __init__(self, **kw):
        self.name = kw.get("name", "Anaya Foods")
        self.category = kw.get("category", "sweets")
        self.palette = kw.get("palette")
        self.fonts = None
        self.template_prefs = kw.get("template_prefs")


def test_the_reference_set_beats_the_category_guess():
    """The category picks a look from a word. The reference set is our own team's
    finished work for THIS brand, so it is the better evidence."""
    brand = _Brand(category="sweets")
    assert brandkit.pick("sweets").key == "warm"
    kit = refstyle.aggregate([_ref(layout="frame_card", light="bright_airy")] * 3)
    report = brandkit.seed_from_references(brand, kit)
    prefs = brand.template_prefs
    assert prefs["family"][0] == "frame_card"
    assert prefs["shoot"] == "bright_airy"
    assert prefs["look"] == report["look"] == "editorial"
    assert brand.fonts["heading"] == brandkit.LOOKS["editorial"].heading
    assert prefs["lessons"] == kit.lessons


def test_the_measured_logo_colour_survives_a_reference_set_that_disagrees():
    """The logo palette is counted pixels; the reference palette is a model reading
    a compressed post. Where they differ the pixels win the primary."""
    brand = _Brand(palette={"primary": "#123B2E", "accent": "#E4572E", "ink": "#FFFFFF"})
    kit = refstyle.aggregate([_ref(palette=["#C81E5B"])] * 3)
    report = brandkit.seed_from_references(brand, kit)
    assert brand.palette["primary"] == "#123B2E"
    assert brand.palette["accent"] == "#C81E5B"
    assert "kept" in report["palette"]

    agreeing = _Brand(palette={"primary": "#1F5B3D", "ink": "#FFFFFF"})
    report = brandkit.seed_from_references(agreeing, refstyle.aggregate([_ref()] * 3))
    assert agreeing.palette["primary"] == "#1F5B3D"
    assert report["palette"] == "confirmed by the reference set"


def test_a_brand_with_no_logo_yet_takes_the_reference_colours():
    brand = _Brand(palette=None)
    brandkit.seed_from_references(brand, refstyle.aggregate([_ref()] * 3))
    assert brand.palette["primary"] == "#1F5B3D" and brand.palette["accent"] == "#F3E7D3"


# --------------------------------------------------------------------------- #
# the staff command: both kinds of folder, and the same run twice
# --------------------------------------------------------------------------- #
BRAND = "11111111-1111-1111-1111-111111111111"
DRIVE_URL = "https://drive.google.com/drive/folders/1AbC_defGHIjkl"


def _folder(root, files: dict[str, bytes]):
    root.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (root / name).write_bytes(data)
    (root / "notes.txt").write_text("not an image")
    return str(root)


class _Resp:
    def __init__(self, status=200, payload=None, content=b""):
        self.status_code = status
        self._payload = payload or {}
        self.content = content

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Drive:
    """One public Drive folder, faked at the HTTP layer so the real query, the
    real listing and the real refusal path all run."""

    def __init__(self, files: dict[str, bytes], status=200):
        self.files = files
        self.status = status

    def __call__(self, timeout=None):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None):
        if (params or {}).get("alt") == "media":
            return _Resp(content=self.files[url.rsplit("/", 1)[1]])
        if self.status >= 400:
            return _Resp(self.status)
        listing = [{"id": n, "name": n, "mimeType": "image/jpeg"} for n in self.files]
        listing.append({"id": "notes.txt", "name": "notes.txt", "mimeType": "text/plain"})
        return _Resp(payload={"files": listing})


def test_a_drive_folder_and_a_local_folder_import_the_same_way(tmp_path, monkeypatch):
    files = {
        "coconut_oil-500ml.jpg": _image(),
        "kadai.jpg": _image(colour=(120, 140, 160)),
    }
    local = onboard.read_source(_folder(tmp_path / "products", files))

    monkeypatch.setattr(onboard.settings, "google_api_key", "test-key")
    monkeypatch.setattr(onboard.httpx, "Client", _Drive(files))
    drive = onboard.read_source(DRIVE_URL)

    assert [f.name for f in drive] == [f.name for f in local] == sorted(files)
    from_local = onboard.plan_products(local, brand_id=BRAND, known_keys=set())
    from_drive = onboard.plan_products(drive, brand_id=BRAND, known_keys=set())
    assert [i.key for i in from_local.stored] == [i.key for i in from_drive.stored]
    # The filename is the label until the vision pass says otherwise, because
    # the free photo lane matches the owner's words against it.
    assert [i.label for i in from_local.stored] == ["coconut oil 500ml", "kadai"]


def test_a_drive_link_with_no_key_stops_the_run_instead_of_half_importing(monkeypatch):
    monkeypatch.setattr(onboard.settings, "google_api_key", "")
    with pytest.raises(onboard.SourceError) as exc:
        onboard.read_source(DRIVE_URL)
    assert "GOOGLE_API_KEY" in str(exc.value)


def test_a_folder_that_is_not_public_is_named_not_guessed(monkeypatch):
    monkeypatch.setattr(onboard.settings, "google_api_key", "test-key")
    monkeypatch.setattr(onboard.httpx, "Client", _Drive({}, status=404))
    with pytest.raises(onboard.SourceError) as exc:
        onboard.read_source(DRIVE_URL)
    assert "anyone with the link" in str(exc.value)
    with pytest.raises(onboard.SourceError):
        onboard.read_source("https://drive.google.com/file/d/")


def test_a_second_run_stores_nothing_twice(tmp_path):
    files = {"jar.jpg": _image(), "shopfront.jpg": _image(colour=(90, 130, 150))}
    src = onboard.read_source(_folder(tmp_path / "p", files))
    first = onboard.plan_products(src, brand_id=BRAND, known_keys=set())
    assert len(first.stored) == 2

    again = onboard.plan_products(src, brand_id=BRAND, known_keys={i.key for i in first.stored})
    assert again.stored == []
    assert [n.reason for n in again.skipped] == ["already stored for this brand"] * 2


def test_the_same_photo_under_two_names_is_stored_once(tmp_path):
    blob = _image()
    src = onboard.read_source(_folder(tmp_path / "p", {"jar.jpg": blob, "jar-copy.jpg": blob}))
    plan = onboard.plan_products(src, brand_id=BRAND, known_keys=set())
    assert len(plan.stored) == 1
    assert plan.skipped[0].reason == "the same file as jar-copy.jpg"


def test_a_photo_too_small_or_too_soft_is_refused_with_a_reason_and_nothing_stored(tmp_path):
    """A soft or tiny photograph cannot be rescued downstream, and the moment to
    ask for a retake is while we are still onboarding the client."""
    from PIL import ImageFilter

    def blurred() -> bytes:
        im = Image.open(io.BytesIO(_image())).filter(ImageFilter.GaussianBlur(4))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=95)
        return out.getvalue()

    src = onboard.read_source(
        _folder(
            tmp_path / "p",
            {"tiny.jpg": _image(size=(400, 500)), "soft.jpg": blurred(), "good.jpg": _image()},
        )
    )
    plan = onboard.plan_products(src, brand_id=BRAND, known_keys=set())
    assert [i.name for i in plan.stored] == ["good.jpg"]
    reasons = {n.name: n.reason for n in plan.rejected}
    assert "400x500px" in reasons["tiny.jpg"] and "document" in reasons["tiny.jpg"]
    assert "soft focus" in reasons["soft.jpg"] and "retake" in reasons["soft.jpg"]


# --------------------------------------------------------------------------- #
# the vision passes, and what happens without them
# --------------------------------------------------------------------------- #
def _items(tmp_path, names: dict[str, bytes], kind="product"):
    src = onboard.read_source(_folder(tmp_path, names))
    return [onboard._prepare(f, BRAND, kind) for f in src]


async def test_with_no_model_the_photos_still_import_and_the_style_pass_is_skipped(
    tmp_path, monkeypatch
):
    """The free owner-photo lane is the point of the products folder and it needs
    no vision model. Losing the labels is a far smaller loss than losing the
    photos, so this one pass is fail-soft."""
    monkeypatch.setattr(onboard.settings, "anthropic_api_key", "")
    monkeypatch.setattr(onboard.settings, "anthropic_model", "")
    items = _items(tmp_path / "p", {"coconut_oil-500ml.jpg": _image()})
    notes = await onboard.label_photos(items)
    assert items[0].kind == "product" and items[0].label == "coconut oil 500ml"
    assert notes and "no vision model" in notes[0]

    refs = _items(tmp_path / "r", {"post1.jpg": _image()}, kind="reference")
    described, refused = await onboard.read_references(refs)
    assert described == [] and refused.rejected == []


async def test_a_photo_that_cannot_be_cut_out_is_never_filed_as_a_product(tmp_path, monkeypatch):
    """Anything filed as 'product' gets cut out and re-staged by the product lane.
    A plate, a set or glass comes back with a hole in it."""
    monkeypatch.setattr(onboard.settings, "anthropic_api_key", "k")
    monkeypatch.setattr(onboard.settings, "anthropic_model", "m")

    async def _seen(data, mime):
        return {"kind": "product", "label": "thali of sweets", "cut_out_ok": False}

    monkeypatch.setattr(onboard.logo_analysis, "describe_photo", _seen)
    items = _items(tmp_path / "p", {"thali.jpg": _image()})
    assert await onboard.label_photos(items) == []
    assert items[0].kind == "other" and items[0].label == "thali of sweets"


async def test_a_reference_the_style_pass_cannot_read_is_refused_and_not_stored(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(onboard.settings, "anthropic_api_key", "k")
    monkeypatch.setattr(onboard.settings, "anthropic_model", "m")

    async def _describe(data):
        raise refstyle.ReferenceUnreadable("layout is not one of the six templates")

    monkeypatch.setattr(onboard.refstyle, "describe", _describe)
    refs = _items(tmp_path / "r", {"post1.jpg": _image()}, kind="reference")
    described, refused = await onboard.read_references(refs)
    assert described == []
    assert refused.rejected[0].name == "post1.jpg"


def test_a_reference_already_on_file_is_read_again_until_it_has_a_style_anchor(tmp_path):
    """A first run on a machine with no vision model must not lock the brand kit
    out: the file is on file, the style anchor is not, so the next run reads it."""
    src = onboard.read_source(_folder(tmp_path / "r", {"post1.jpg": _image()}))
    plan = onboard.plan_references(src, brand_id=BRAND, known_keys=set(), known_refs=set())
    item = plan.stored[0]
    assert item.kind == "reference" and not item.already_stored

    second = onboard.plan_references(src, brand_id=BRAND, known_keys={item.key}, known_refs=set())
    assert second.stored[0].already_stored is True

    third = onboard.plan_references(
        src, brand_id=BRAND, known_keys={item.key}, known_refs={onboard.memory_ref(item)}
    )
    assert third.stored == [] and third.skipped[0].reason == "already stored and already read"


# --------------------------------------------------------------------------- #
# what the agent is told about the seeded set
# --------------------------------------------------------------------------- #
def _seeded_brand():
    brand = _Brand(palette={"primary": "#123B2E", "ink": "#FFFFFF"})
    brandkit.seed_from_references(
        brand, refstyle.aggregate([_ref(layout="frame_card", light="moody")] * 3)
    )
    brand.never_say = ["cures", "100% natural"]
    brand.languages = ["hi", "en"]
    return brand


def test_the_seeded_rules_are_in_the_prompt_and_are_not_similarity_gated():
    """They are the visual twin of never_say. Retrieval ranks memories against the
    owner's message, and "their posts set the words on a solid panel" resembles
    nothing an owner ever types, so a retrieved version would never be seen --
    on exactly the creatives that have no other guidance: the first ones."""
    from app.agent import prompts

    brand = _seeded_brand()
    system = prompts.build_system(brand, memory_block="", extra="")
    for rule in brand.template_prefs["lessons"]:
        assert rule in system
    assert "standing instruction" in system
    # Beside never_say, in the same block, both hard.
    assert "NEVER use these words" in system
    # And the layout family our own team used, not the one the category guessed.
    assert "Preferred layouts: frame_card" in system


def test_the_prompt_says_nothing_of_the_kind_for_a_brand_with_no_reference_set():
    from app.agent import prompts

    brand = _Brand(palette={"primary": "#123B2E"})
    brand.never_say = []
    brand.languages = []
    brand.template_prefs = {"look": "warm"}
    system = prompts.build_system(brand)
    assert "standing instruction" not in system


def test_the_seeded_rules_stay_short_enough_to_read():
    """A longer list reads as a wall and gets skimmed; the rules that matter are
    written first."""
    from app.agent import prompts

    brand = _seeded_brand()
    brand.template_prefs = {**brand.template_prefs, "lessons": [f"rule {i}" for i in range(12)]}
    system = prompts.build_system(brand)
    assert "rule 5" in system and "rule 6" not in system
    assert prompts.MAX_SEEDED_RULES == 6


def test_an_owner_who_asks_for_another_look_is_not_left_with_the_seeded_family():
    """The family belongs to the look. Left behind, it would keep pointing at a
    layout the look they just asked for does not use."""
    brand = _Brand(palette={"primary": "#123B2E"})
    brandkit.seed_from_references(brand, refstyle.aggregate([_ref(layout="frame_card")] * 3))
    assert brand.template_prefs["family"][0] == "frame_card"

    brandkit.apply(brand, "bold")
    assert "family" not in brand.template_prefs
    assert brand.template_prefs["look"] == "bold"
    # What the set actually showed is still true of their own posts.
    assert brand.template_prefs["lessons"]


# --------------------------------------------------------------------------- #
# the whole run, with only the database and R2 faked
# --------------------------------------------------------------------------- #
class _FakeDb:
    def __init__(self, brand, rows):
        self.brand = brand
        self.rows = rows

    def get(self, model, key):
        return self.brand

    def scalars(self, stmt):
        return types.SimpleNamespace(all=lambda: [])

    def add(self, row):
        self.rows.append(row)

    def flush(self):
        pass


def _fake_session(brand, rows):
    @contextmanager
    def scope():
        yield _FakeDb(brand, rows)

    return scope


def _run_args(tmp_path, dry_run):
    products = _folder(tmp_path / "p", {"coconut_oil-500ml.jpg": _image()})
    refs = _folder(tmp_path / "r", {"post1.jpg": _image(colour=(90, 120, 100))})
    return argparse.Namespace(brand=BRAND, refs=refs, products=products, dry_run=dry_run)


async def test_a_brand_with_no_model_still_gets_its_photos_and_is_told_why_not_the_style(
    tmp_path, monkeypatch, capsys
):
    """Our team's photos are most of the value and need no vision model at all.
    A run that refused them because the style pass could not run would leave the
    client with nothing on the day they paid."""
    brand = _Brand(palette={"primary": "#123B2E"})
    brand.template_prefs = {}
    rows: list = []
    monkeypatch.setattr(onboard, "session_scope", _fake_session(brand, rows))
    monkeypatch.setattr(onboard.r2, "put", lambda key, data, mime=None: f"https://cdn.test/{key}")
    monkeypatch.setattr(onboard.settings, "anthropic_api_key", "")
    monkeypatch.setattr(onboard.settings, "anthropic_model", "")

    code = await onboard.run(_run_args(tmp_path, dry_run=False))
    out = capsys.readouterr().out
    assert code == 0
    assert "the style pass was skipped" in out
    assert [r.kind for r in rows] == ["product", "reference"]
    # The kit is not guessed at from nothing: it stays as it was.
    assert brand.template_prefs == {}
    assert "unchanged (no reference creative was read)" in out


async def test_a_dry_run_writes_nothing_at_all(tmp_path, monkeypatch, capsys):
    brand = _Brand(palette={"primary": "#123B2E"})
    brand.template_prefs = {}
    rows: list = []
    monkeypatch.setattr(onboard, "session_scope", _fake_session(brand, rows))
    monkeypatch.setattr(onboard.settings, "anthropic_api_key", "k")
    monkeypatch.setattr(onboard.settings, "anthropic_model", "m")

    async def _describe(data):
        return _ref(layout="frame_card", light="moody")

    def _no(*a, **kw):
        raise AssertionError("a dry run must not touch R2")

    monkeypatch.setattr(onboard.refstyle, "describe", _describe)
    monkeypatch.setattr(onboard.logo_analysis, "describe_photo", _describe_photo)
    monkeypatch.setattr(onboard.r2, "put", _no)

    code = await onboard.run(_run_args(tmp_path, dry_run=True))
    out = capsys.readouterr().out
    assert code == 0 and rows == []
    assert brand.template_prefs == {}
    # ...but it still shows the kit it would set, which is what a dry run is for.
    assert "frame_card" in out and "DRY RUN" in out


async def _describe_photo(data, mime):
    return {"kind": "product", "label": "cold pressed coconut oil 500ml", "cut_out_ok": True}
