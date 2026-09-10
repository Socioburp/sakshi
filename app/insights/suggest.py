"""Propose today's post before the owner has to think of one.

The owner said, in the founder interviews, that they do not want to think
about marketing every day. This is where that promise is kept. A suggestion
is assembled from four things that are already known -- the calendar, the
weekday, the product photos nobody has used yet, and what this owner has
approved before -- ranked, and handed to the agent as a ready brief sketch
with a reason a shop owner would nod at.

No model is involved in choosing. The agent turns the winning sketch into
copy in the owner's language; the choice itself is deterministic, so two
owners with the same shop on the same day get the same reasoning, and a
wrong suggestion can be traced to a rule rather than to a mood.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.creative import grid
from app.creative.photoreal import category_direction
from app.db.models import Brand, BrandAsset, Brief, CreativeEvent
from app.insights.profile import Taste, taste

FESTIVALS = Path(__file__).resolve().parents[2] / "docs" / "festivals_in.json"

# Festival within this many days is the strongest reason to post. Owners need
# a few days of runway: a Diwali offer on Diwali evening is a wasted post.
LEAD_DAYS = 7
# Do not propose the same intent that the last two creatives already used.
REPEAT_WINDOW = 2

# Educational carousel angles by category keyword. Carousels earn saves; a
# shop that only posts offers trains its followers to scroll past.
HOWTO: list[tuple[tuple[str, ...], list[str]]] = [
    (
        ("oil", "ghee", "spice", "pickle", "food", "sweet", "snack", "tea", "coffee"),
        [
            "3 ways to use it in everyday cooking",
            "how to store it so it stays fresh",
            "what to look for when you buy",
        ],
    ),
    (
        ("skincare", "cosmetic", "beauty", "serum", "soap", "hair"),
        [
            "a 3-step routine for the season",
            "3 mistakes people make with it",
            "what is inside and why it matters",
        ],
    ),
    (
        ("cloth", "apparel", "boutique", "saree", "kurta", "fashion"),
        ["3 ways to style it", "how to care for the fabric", "what to wear to the next occasion"],
    ),
    (
        ("jewel", "gold", "silver"),
        ["how to keep it shining", "3 pieces every wardrobe needs", "how to read a hallmark"],
    ),
    (
        ("gym", "fitness", "clinic", "dental", "coaching", "class"),
        [
            "3 small habits that work",
            "questions people ask us most",
            "what a first visit looks like",
        ],
    ),
]
GENERIC_HOWTO = ["3 things customers ask us most", "how we make it", "before and after"]


@dataclass
class Idea:
    rank: int
    why: str
    intent: str
    headline_idea: str
    format: str = "single"
    slide_count: int = 1
    template: str = "lower_third"
    aspect_ratio: str = "4:5"
    visual_direction: str = ""
    mood: str = ""
    reference_asset_id: str | None = None
    festival: str | None = None
    hashtags_hint: list[str] = field(default_factory=list)
    plan_slot: str | None = None  # the plan date this idea fulfils, if any

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_festivals(path: Path = FESTIVALS) -> list[dict[str, Any]]:
    try:
        return json.loads(path.read_text())["festivals"]
    except Exception:  # noqa: BLE001 - a broken calendar must not break the day
        return []


def upcoming_festivals(today: date, lead: int = LEAD_DAYS) -> list[tuple[int, dict[str, Any]]]:
    out = []
    for f in load_festivals():
        try:
            d = date.fromisoformat(f["date"])
        except (KeyError, ValueError):
            continue
        delta = (d - today).days
        # The day itself is too late to sell for; a greeting is the agent's call.
        if 1 <= delta <= lead:
            out.append((delta, f))
    return sorted(out, key=lambda x: x[0])


def _howto_angles(category: str | None) -> list[str]:
    from app.creative.photoreal import _has_word

    cat = (category or "").lower()
    for keys, angles in HOWTO:
        if any(_has_word(cat, k) for k in keys):
            return angles
    return GENERIC_HOWTO


def _recent_intents(db: Session, brand_id: uuid.UUID, n: int = REPEAT_WINDOW) -> list[str]:
    rows = db.scalars(
        select(Brief).where(Brief.brand_id == brand_id).order_by(Brief.created_at.desc()).limit(n)
    ).all()
    return [str((r.payload or {}).get("intent") or "") for r in rows]


def _unused_photos(db: Session, brand_id: uuid.UUID) -> list[BrandAsset]:
    used = set()
    for r in db.scalars(
        select(Brief).where(Brief.brand_id == brand_id).order_by(Brief.created_at.desc()).limit(60)
    ):
        p = r.payload or {}
        vd = p.get("visual_direction") or {}
        if vd.get("reference_asset_id"):
            used.add(str(vd["reference_asset_id"]))
        for s in p.get("slides") or []:
            ref = (s.get("visual_direction") or {}).get("reference_asset_id")
            if ref:
                used.add(str(ref))
    photos = db.scalars(
        select(BrandAsset)
        .where(BrandAsset.brand_id == brand_id, BrandAsset.kind == "product")
        .order_by(BrandAsset.created_at.desc())
        .limit(30)
    ).all()
    return [a for a in photos if str(a.id) not in used]


def _weekday_hook(today: date) -> tuple[str, str, str] | None:
    """(intent, headline idea, why) for the day of the week, or None."""
    wd = today.weekday()
    if wd in (4, 5):  # Friday, Saturday
        return ("promo", "Weekend special", "weekend footfall: offers posted Fri/Sat get acted on")
    if today.day <= 5:
        return (
            "promo",
            "New month, new stock",
            "first days of the month are payday for most customers",
        )
    if wd == 0:
        return (
            "behind_the_scenes",
            "How we start the week",
            "Monday behind-the-scenes builds trust cheaply",
        )
    if wd == 6:
        return (
            "testimonial",
            "From our customers",
            "Sunday is when people browse, not buy: a review post",
        )
    return None


def _prefer(taste_: Taste, key: str, default: str) -> str:
    scores = {"template": taste_.template_scores, "aspect": taste_.aspect_scores}[key]
    return taste_.best(scores) or default


def suggest(db: Session, brand: Brand, *, today: date | None = None, limit: int = 3) -> list[Idea]:
    """Ranked ideas for today, most compelling first."""
    today = today or datetime.now(ZoneInfo("Asia/Kolkata")).date()
    t = taste(db, brand.id)
    # Defaults come from what they have approved: the vote history first, the
    # approved-post signature second. Otherwise the guard would refuse the
    # bot's own idea.
    fp = grid.fingerprint(db, brand.id)
    template = _prefer(t, "template", fp.dominant(fp.templates) or "lower_third")
    aspect = _prefer(t, "aspect", fp.dominant(fp.aspects) or "4:5")
    play = category_direction(brand.category)
    recent = _recent_intents(db, brand.id)
    ideas: list[Idea] = []

    # The month's plan comes first: a slot planned for today (or missed
    # earlier this week) is the idea, and the rest are alternatives.
    from app.insights import plan as planning

    slot = planning.slot_for(planning.current(db, brand.id, today), today)
    if slot:
        ideas.append(
            Idea(**planning.idea_from_slot(slot, template=template, aspect=aspect, play=play))
        )

    for delta, f in upcoming_festivals(today)[:1]:
        when = f"in {delta} day{'s' if delta != 1 else ''}"
        ideas.append(
            Idea(
                rank=0,
                why=(
                    f"{f['name']} is {when}; posts before a festival sell, "
                    "posts on the day only greet"
                ),
                intent="festive",
                headline_idea=f"{f['name']} special",
                template=template,
                aspect_ratio=aspect,
                visual_direction=(
                    f"the product styled for {f['name']}: {f.get('angle', 'festive')}, "
                    f"{play or 'natural light, real surfaces'}"
                ),
                mood="festive, warm",
                festival=f["name"],
            )
        )

    for photo in _unused_photos(db, brand.id)[:1]:
        label = photo.label or "the product"
        ideas.append(
            Idea(
                rank=0,
                why=f"you sent a photo of {label} that has not been used in a post yet",
                intent="new_product",
                headline_idea=(label[:40] or "Our product"),
                template=template,
                aspect_ratio=aspect,
                visual_direction=(
                    f"the owner's own photo of {label}, product kept exactly as photographed"
                ),
                mood="clean, honest",
                reference_asset_id=str(photo.id),
            )
        )

    angles = _howto_angles(brand.category)
    idx = today.toordinal() % len(angles)
    ideas.append(
        Idea(
            rank=0,
            why=(
                "a how-to carousel gets saved and shared; offers alone train followers to "
                "scroll past"
            ),
            intent="educational",
            headline_idea=angles[idx],
            format="carousel",
            slide_count=3,
            template=template,
            aspect_ratio=aspect,
            visual_direction=f"three close, useful photographs that show each step; {play}".rstrip(
                "; "
            ),
            mood="useful, calm",
        )
    )

    hook = _weekday_hook(today)
    if hook:
        intent, headline, why = hook
        ideas.append(
            Idea(
                rank=0,
                why=why,
                intent=intent,
                headline_idea=headline,
                template=template,
                aspect_ratio=aspect,
                visual_direction=f"{play or 'a real moment at the shop, natural light'}",
                mood="warm, real",
            )
        )

    # Rank: festival first, then the unused photo, then whatever does not
    # repeat what they just posted.
    def key(i: Idea) -> tuple[int, int]:
        if i.plan_slot:
            return (-1, -1)
        repeat = 1 if i.intent in recent and not i.festival else 0
        base = 0 if i.festival else 1 if i.reference_asset_id else 2
        return (repeat, base)

    ideas.sort(key=key)
    for n, i in enumerate(ideas[:limit], start=1):
        i.rank = n
    return ideas[:limit]


def record_suggested(db: Session, brand: Brand, ideas: list[Idea]) -> None:
    from app.insights.events import record

    record(
        db,
        kind="suggested",
        account_id=brand.account_id,
        brand_id=brand.id,
        meta={"ideas": [i.as_dict() for i in ideas]},
    )


def suggested_recently(db: Session, brand_id: uuid.UUID, hours: int = 20) -> bool:
    row = db.scalar(
        select(CreativeEvent)
        .where(
            CreativeEvent.brand_id == brand_id,
            CreativeEvent.kind == "suggested",
            CreativeEvent.created_at >= datetime.now(UTC) - timedelta(hours=hours),
        )
        .limit(1)
    )
    return row is not None
