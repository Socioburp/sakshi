"""The month, planned before it starts.

An agency runs a calendar, not a queue of requests. The plan has four parts
and they are decided in this order:

1. A goal for the month -- footfall, leads, a launch, or awareness. It sets
   the pillar mix: an offer-heavy month for footfall, education and proof
   for leads, a teaser -> launch -> reminder arc for a launch.
2. A cadence: how many posts a week the owner will actually approve. Four
   is the default; a plan the owner cannot keep up with is a plan that
   makes them feel behind, which is the fastest way to churn.
3. Campaign arcs for the festivals in the month: teaser five days before,
   the offer two days before, "last day" the day before. Posts on the day
   itself only greet.
4. The remaining slots filled by pillar, weighted, never two of the same
   pillar in a row, each with an idea the agent can build directly.

The plan is stored whole (content_plans.slots) and read whole: today's slot
is idea #1 in `suggest`, and the Monday message lists the week.
"""

from __future__ import annotations

import uuid
from calendar import monthrange
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Brand, BrandAsset, ContentPlan
from app.insights.suggest import load_festivals

GOALS = ("footfall", "leads", "launch", "awareness")

# pillar -> the brief intent it is built with
PILLAR_INTENT = {
    "product": "new_product",
    "offer": "promo",
    "proof": "testimonial",
    "education": "educational",
    "behind_the_scenes": "behind_the_scenes",
}

# Mix per goal. Sums to 1; the planner rounds to slots.
MIX_BY_GOAL: dict[str, dict[str, float]] = {
    "awareness": {
        "product": 0.30,
        "proof": 0.15,
        "education": 0.25,
        "behind_the_scenes": 0.20,
        "offer": 0.10,
    },
    "footfall": {
        "product": 0.25,
        "proof": 0.15,
        "education": 0.10,
        "behind_the_scenes": 0.15,
        "offer": 0.35,
    },
    "leads": {
        "product": 0.20,
        "proof": 0.30,
        "education": 0.30,
        "behind_the_scenes": 0.10,
        "offer": 0.10,
    },
    "launch": {
        "product": 0.40,
        "proof": 0.15,
        "education": 0.15,
        "behind_the_scenes": 0.20,
        "offer": 0.10,
    },
}

# Posting days by cadence, chosen for when Indian retail audiences act:
# Fri/Sat for offers, Tue/Thu for education, Sunday for stories.
DAYS_BY_CADENCE = {
    1: (5,),  # Sat
    2: (2, 5),  # Wed, Sat
    3: (1, 4, 6),  # Tue, Fri, Sun
    4: (1, 3, 5, 6),  # Tue, Thu, Sat, Sun
    5: (0, 1, 3, 5, 6),
    6: (0, 1, 2, 3, 5, 6),
    7: (0, 1, 2, 3, 4, 5, 6),
}

FESTIVAL_ARC = (
    (5, "education", "festive", "{festival} is coming: how to choose the right {product}"),
    (2, "offer", "festive", "{festival} special: {product}"),
    (1, "offer", "festive", "Last day for {festival} orders"),
)


@dataclass(slots=True)
class Slot:
    date: str  # ISO
    pillar: str
    intent: str
    headline_idea: str
    why: str
    campaign: str | None = None  # festival name for arc posts
    status: str = "planned"  # planned | made | skipped
    brief_id: str | None = None
    format: str = "single"
    slide_count: int = 1
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def month_start(d: date) -> date:
    return d.replace(day=1)


def _products(db: Session, brand_id: uuid.UUID) -> list[str]:
    rows = db.scalars(
        select(BrandAsset)
        .where(BrandAsset.brand_id == brand_id, BrandAsset.kind == "product")
        .order_by(BrandAsset.created_at.desc())
        .limit(12)
    ).all()
    seen: list[str] = []
    for r in rows:
        label = (r.label or "").strip()
        if label and label.lower() not in {s.lower() for s in seen}:
            seen.append(label[:40])
    return seen


def _posting_days(month: date, cadence: int) -> list[date]:
    weekdays = DAYS_BY_CADENCE[max(1, min(7, cadence))]
    _, n = monthrange(month.year, month.month)
    return [
        month.replace(day=d) for d in range(1, n + 1) if month.replace(day=d).weekday() in weekdays
    ]


def _festivals_in(month: date) -> list[dict]:
    _, n = monthrange(month.year, month.month)
    end = month.replace(day=n)
    out = []
    for f in load_festivals():
        try:
            d = date.fromisoformat(f["date"])
        except (KeyError, ValueError):
            continue
        # An arc starts five days before, so a festival in the first days of
        # next month belongs to this month's plan too.
        if month <= d <= end + timedelta(days=5):
            out.append({**f, "_date": d})
    return out


def _weighted_pillars(mix: dict[str, float], n: int) -> list[str]:
    """`n` pillars in the mix's proportions, spread out so none repeats."""
    if n <= 0:
        return []
    counts = {p: int(round(w * n)) for p, w in mix.items()}
    while sum(counts.values()) < n:
        p = max(mix, key=lambda k: mix[k] * n - counts[k])
        counts[p] += 1
    while sum(counts.values()) > n:
        p = max(counts, key=lambda k: counts[k] - mix[k] * n)
        counts[p] -= 1
    out: list[str] = []
    remaining = dict(counts)
    last = None
    for _ in range(n):
        # The pillar furthest behind its share, never the one just used.
        order = sorted(
            (p for p in remaining if remaining[p] > 0),
            key=lambda p: -(remaining[p] / max(mix[p], 1e-6)),
        )
        pick = next((p for p in order if p != last), order[0] if order else "product")
        out.append(pick)
        remaining[pick] = remaining.get(pick, 0) - 1
        last = pick
    return out


def _idea_for(pillar: str, products: list[str], i: int, locality: str | None) -> tuple[str, str]:
    """(headline idea, why) for a pillar slot."""
    product = products[i % len(products)] if products else "our product"
    loc = locality or "your area"
    if pillar == "product":
        return f"{product}: what makes it different", "a product post keeps the catalogue alive"
    if pillar == "offer":
        return (
            f"This week's offer on {product}",
            "offers posted Fri/Sat get acted on over the weekend",
        )
    if pillar == "proof":
        return (
            f"What customers in {loc} say about {product}",
            "proof converts the people who saw the offer and hesitated",
        )
    if pillar == "education":
        return f"3 things to know before buying {product}", "how-to posts get saved and shared"
    return (
        f"How {product} is made",
        "behind-the-scenes builds trust cheaply and reads as real",
    )


def build(
    db: Session,
    brand: Brand,
    month: date,
    *,
    goal: str = "awareness",
    cadence: int = 4,
    launch: str | None = None,
) -> ContentPlan:
    """Write (or rewrite) the plan for `month`. Slots already made are kept."""
    month = month_start(month)
    goal = goal if goal in GOALS else "awareness"
    cadence = max(1, min(7, int(cadence)))
    mix = MIX_BY_GOAL[goal]
    prefs = brand.template_prefs or {}
    products = _products(db, brand.id)
    if launch:
        products = [launch, *products]
    days = _posting_days(month, cadence)

    existing = db.scalar(
        select(ContentPlan).where(ContentPlan.brand_id == brand.id, ContentPlan.month == month)
    )
    made = {
        s["date"]: s
        for s in (existing.slots if existing else [])
        if isinstance(s, dict) and s.get("status") == "made"
    }

    slots: dict[str, Slot] = {}
    # 1. festival arcs claim their days first (they move to the nearest posting
    #    day at or before the arc day so cadence is respected).
    for f in _festivals_in(month):
        for lead, pillar, intent, pattern in FESTIVAL_ARC:
            target = f["_date"] - timedelta(days=lead)
            if target < month or target.month != month.month:
                continue
            product = products[0] if products else "our products"
            key = target.isoformat()
            if key in slots or key in made:
                continue
            slots[key] = Slot(
                date=key,
                pillar=pillar,
                intent=intent,
                headline_idea=pattern.format(festival=f["name"], product=product),
                why=f"{f['name']} is in {lead} day{'s' if lead != 1 else ''}",
                campaign=f["name"],
                extra={"angle": f.get("angle", "")},
            )
    # 2. a launch arc when the owner named one
    if launch and days:
        arc = [
            (0, "product", "announcement", f"Coming soon: {launch}", "a teaser builds the queue"),
            (1, "product", "new_product", f"It's here: {launch}", "the launch post itself"),
            (2, "proof", "testimonial", f"First reactions to {launch}", "proof after a launch"),
        ]
        for offset, pillar, intent, headline, why in arc:
            if offset < len(days):
                key = days[offset].isoformat()
                if key not in slots and key not in made:
                    slots[key] = Slot(key, pillar, intent, headline, why, campaign=launch)
    # 3. the rest by pillar mix
    free = [d for d in days if d.isoformat() not in slots and d.isoformat() not in made]
    pillars = _weighted_pillars(mix, len(free))
    for i, (d, pillar) in enumerate(zip(free, pillars, strict=True)):
        headline, why = _idea_for(pillar, products, i, prefs.get("locality"))
        intent = PILLAR_INTENT[pillar]
        fmt, count = ("carousel", 3) if pillar == "education" else ("single", 1)
        slots[d.isoformat()] = Slot(
            d.isoformat(), pillar, intent, headline, why, format=fmt, slide_count=count
        )

    ordered = sorted([*slots.values()], key=lambda s: s.date)
    payload = [s.as_dict() for s in ordered]
    payload.extend(made.values())
    payload.sort(key=lambda s: s["date"])

    if existing is None:
        existing = ContentPlan(brand_id=brand.id, month=month)
        db.add(existing)
    existing.goal = goal
    existing.cadence = cadence
    existing.pillar_mix = mix
    existing.slots = payload
    db.flush()
    return existing


def current(db: Session, brand_id: uuid.UUID, today: date) -> ContentPlan | None:
    return db.scalar(
        select(ContentPlan).where(
            ContentPlan.brand_id == brand_id, ContentPlan.month == month_start(today)
        )
    )


def slot_for(plan: ContentPlan | None, today: date) -> dict | None:
    """Today's planned slot, or the nearest missed one still this week."""
    if plan is None:
        return None
    key = today.isoformat()
    for s in plan.slots:
        if s.get("date") == key and s.get("status") == "planned":
            return s
    # A slot missed earlier this week is better built late than never.
    week_start = today - timedelta(days=today.weekday())
    for s in sorted(plan.slots, key=lambda x: x["date"], reverse=True):
        d = date.fromisoformat(s["date"])
        if week_start <= d < today and s.get("status") == "planned":
            return s
    return None


def week_of(plan: ContentPlan | None, today: date) -> list[dict]:
    if plan is None:
        return []
    start = today - timedelta(days=today.weekday())
    end = start + timedelta(days=6)
    return [s for s in plan.slots if start.isoformat() <= s["date"] <= end.isoformat()]


def mark(db: Session, brand_id: uuid.UUID, slot_date: str, *, status: str, brief_id=None) -> bool:
    """Flip a slot's status; True when a slot on that date existed."""
    try:
        d = date.fromisoformat(slot_date)
    except ValueError:
        return False
    plan = current(db, brand_id, d)
    if plan is None:
        return False
    hit = False
    new_slots = []
    for s in plan.slots:
        if s.get("date") == slot_date and s.get("status") == "planned":
            s = {**s, "status": status, "brief_id": str(brief_id) if brief_id else None}
            hit = True
        new_slots.append(s)
    if hit:
        plan.slots = new_slots
        db.flush()
    return hit


def week_message(plan: ContentPlan | None, today: date, lang: str = "en") -> str | None:
    """The Monday line-up, one line per slot."""
    week = [s for s in week_of(plan, today) if s.get("status") != "skipped"]
    if not week:
        return None
    head = {
        "hi": "Is hafte ka plan:",
        "en": "This week's plan:",
    }.get(lang, "This week's plan:")
    lines = [head]
    for s in week:
        d = date.fromisoformat(s["date"])
        tick = "✓ " if s.get("status") == "made" else ""
        lines.append(f"{d.strftime('%a %d')}: {tick}{s['headline_idea']}")
    tail = {
        "hi": "Koi bhi badalna ho toh bata dijiye.",
        "en": "Say the word to change any of them.",
    }
    lines.append(tail.get(lang, tail["en"]))
    return "\n".join(lines)


def describe(plan: ContentPlan) -> dict[str, Any]:
    made = sum(1 for s in plan.slots if s.get("status") == "made")
    return {
        "month": plan.month.isoformat(),
        "goal": plan.goal,
        "cadence_per_week": plan.cadence,
        "posts_planned": len(plan.slots),
        "posts_made": made,
        "next": next((s for s in plan.slots if s.get("status") == "planned"), None),
        "campaigns": sorted({s["campaign"] for s in plan.slots if s.get("campaign")}),
    }


def idea_from_slot(slot: dict, *, template: str, aspect: str, play: str) -> dict[str, Any]:
    """A suggest.Idea-shaped dict for today's slot, ranked first."""
    return {
        "rank": 1,
        "why": f"planned for today: {slot['why']}",
        "intent": slot["intent"],
        "headline_idea": slot["headline_idea"],
        "format": slot.get("format", "single"),
        "slide_count": slot.get("slide_count", 1),
        "template": template,
        "aspect_ratio": aspect,
        "visual_direction": play or "a real moment at the shop, natural light, real surfaces",
        "mood": "warm, real",
        "reference_asset_id": None,
        "festival": slot.get("campaign"),
        "hashtags_hint": [],
        "plan_slot": slot["date"],
    }
