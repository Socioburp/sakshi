"""The festival post, offered before they think of it -- and built on THEIR shop.

Every festival app in India sells one promise: "never miss a festival". They
keep it with a catalogue of generic frames the owner has to open, browse and
stamp a logo on. Sakshi had the better half of this already -- it knows what
the shop sells, has their photographs, knows the layout their posts share --
and never used it at the moment it matters, because the daily nudge only
fires inside WhatsApp's 24-hour window. An owner who last wrote a week ago
(i.e. most owners, three days before Diwali) heard nothing.

This closes that:

  * a sweep, once a morning, finds brands with a festival a few days out;
  * the offer is about THEM: "Dussehra is in 3 days -- shall I make the post
    with your motichoor box photo, in your usual style?" The idea it carries
    points at their own photograph, so tapping "Make it" costs them nothing
    and costs us nothing (the owner-photo lane makes no image call);
  * inside the 24h window it is an ordinary message with buttons. Outside it,
    it is a WhatsApp TEMPLATE message -- the only thing Meta delivers there.

WHAT THIS COSTS, AND THE RULES THAT FOLLOW FROM IT. A template message outside
the window is a paid marketing conversation (about Rs 1 in India). So:

  * off unless WA_TEMPLATE_FESTIVAL names an approved template;
  * one push per brand per festival, ever;
  * FESTIVAL_PUSH_MONTHLY_CAP paid pushes per brand per month;
  * never to an owner who switched nudges off or snoozed them;
  * never for a festival whose date is not verified. The calendar marks
    lunar dates it has not confirmed with `"verify": true`; a wrong "tomorrow
    is Onam" is worse than silence, so those are skipped until a human
    removes the flag.

The choice of festival and of photograph is deterministic, like suggest.py.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Account, Brand, BrandAsset, CreativeEvent, WaSession
from app.insights import suggest as sg
from app.logging import get_logger

log = get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")
# Far enough out to make, approve and post BEFORE the day; near enough to feel
# timely. suggest.py's own rule: "posts before a festival sell, posts on the
# day only greet".
PUSH_LEAD_DAYS = 3
# The sweep enqueues during these IST hours; shops are open and owners are on
# their phones, and nobody is woken by a marketing assistant.
SWEEP_HOURS = range(9, 13)
# How long a pushed idea stays tappable after the window it was sent in closes.
IDEA_TTL = timedelta(hours=72)


@dataclass(frozen=True)
class Due:
    name: str
    day: date
    days_away: int
    angle: str

    @property
    def key(self) -> str:
        return f"{self.day.isoformat()}:{self.name}"


def due_festival(today: date, locale: str | None, lead: int = PUSH_LEAD_DAYS) -> Due | None:
    """The nearest VERIFIED festival 1..lead days away that this owner keeps.

    A festival entry may carry `"languages": ["ta", "ml"]`; it is then offered
    only to owners writing in one of them -- Pongal to a Tamil shop, not to a
    Punjabi one. An entry with no list is national.
    """
    lang = ((locale or "en").split("-")[0]).lower()
    for delta, f in sg.upcoming_festivals(today, lead):
        if f.get("verify"):
            continue
        langs = [str(x).lower() for x in (f.get("languages") or [])]
        if langs and lang not in langs:
            continue
        return Due(f["name"], date.fromisoformat(f["date"]), delta, f.get("angle", "festive"))
    return None


def best_photo(db: Session, brand_id: uuid.UUID) -> BrandAsset | None:
    """Their own product photo to build the post on: one not used yet if there
    is one (suggest.py's rule), else the newest."""
    unused = sg._unused_photos(db, brand_id)
    if unused:
        return unused[0]
    return db.scalar(
        select(BrandAsset)
        .where(BrandAsset.brand_id == brand_id, BrandAsset.kind == "product")
        .order_by(BrandAsset.created_at.desc())
        .limit(1)
    )


def already_pushed(db: Session, brand_id: uuid.UUID, due: Due) -> bool:
    rows = db.scalars(
        select(CreativeEvent)
        .where(
            CreativeEvent.brand_id == brand_id,
            CreativeEvent.kind == "suggested",
            CreativeEvent.created_at >= datetime.now(UTC) - timedelta(days=30),
        )
        .order_by(CreativeEvent.created_at.desc())
        .limit(60)
    ).all()
    return any((r.meta or {}).get("festival_push") == due.key for r in rows)


def paid_pushes_this_month(db: Session, brand_id: uuid.UUID, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = db.scalars(
        select(CreativeEvent).where(
            CreativeEvent.brand_id == brand_id,
            CreativeEvent.kind == "suggested",
            CreativeEvent.created_at >= start,
        )
    ).all()
    return sum(1 for r in rows if (r.meta or {}).get("paid_template"))


def build_offer(db: Session, brand: Brand, due: Due, today: date) -> dict[str, Any]:
    """The idea (as suggest.Idea dicts) and the brand facts the line is made of."""
    ideas = sg.suggest(db, brand, today=today, limit=3)
    festive = next((i for i in ideas if i.festival == due.name), None)
    if festive is None:  # the month's plan outranked it; the push is still about the festival
        festive = sg.Idea(
            rank=1,
            why=f"{due.name} is in {due.days_away} days",
            intent="festive",
            headline_idea=f"{due.name} special",
            visual_direction=f"the product styled for {due.name}: {due.angle}",
            mood="festive, warm",
            festival=due.name,
        )
    photo = best_photo(db, brand.id)
    if photo is not None:
        # Their own photograph: the post is unmistakably theirs, and the
        # owner-photo lane makes no image call -- free for them and for us.
        festive.reference_asset_id = str(photo.id)
        festive.visual_direction = (
            f"the owner's own photo of {photo.label or 'the product'}, kept exactly as "
            f"photographed, styled for {due.name}"
        )
    from app.creative import grid

    fp = grid.fingerprint(db, brand.id)
    ordered = [festive] + [i for i in ideas if i is not festive][:2]
    for n, idea in enumerate(ordered, start=1):
        idea.rank = n
    return {
        "ideas": [i.as_dict() for i in ordered],
        "photo_label": (photo.label if photo is not None else None),
        "usual_style": bool(fp.enough),
        "free": photo is not None,
    }


_LINE = {
    "en": "{festival} is {when}. Shall I make your post{photo}{style}?",
    "hi": "{festival} {when} hai. Aapka post bana doon{photo}{style}?",
}
_WHEN = {
    "en": lambda n: "tomorrow" if n == 1 else f"in {n} days",
    "hi": lambda n: "kal" if n == 1 else f"{n} din mein",
}
_PHOTO = {"en": " with your {label} photo", "hi": " aapki {label} wali photo se"}
_STYLE = {"en": ", in your usual style", "hi": ", aapke usual style mein"}
# Deliberately NOT said: "no credit needed". It is true when the post is built
# on their own photo -- but the agent writes the brief, and a promise about
# price made here and broken there is the kind the owner remembers.


def line_for(due: Due, offer: dict[str, Any], lang: str) -> str:
    """The plain line. Every clause in it is a fact from `offer` -- a photo is
    mentioned only if there is one, their style only if they have one."""
    lang = lang if lang in _LINE else "en"
    label = (offer.get("photo_label") or "").strip()
    return _LINE[lang].format(
        festival=due.name,
        when=_WHEN[lang](due.days_away),
        photo=_PHOTO[lang].format(label=label[:40]) if label else "",
        style=_STYLE[lang] if offer.get("usual_style") else "",
    )


def template_params(due: Due, offer: dict[str, Any], brand_name: str) -> list[str]:
    """Body parameters for the approved template, in order:
    {{1}} festival  {{2}} days away  {{3}} what the post is built on."""
    label = (offer.get("photo_label") or "").strip()
    return [due.name, str(due.days_away), (f"your {label} photo" if label else brand_name)[:60]]


# --------------------------------------------------------------------------- #
# the sweep
# --------------------------------------------------------------------------- #
def eligible(brand: Brand, account: Account | None) -> bool:
    from app.insights import votes

    if account is None or account.blocked_at is not None:
        return False
    prefs = brand.template_prefs or {}
    if prefs.get("daily_nudge") is False or prefs.get("festival_push") is False:
        return False
    return not votes.nudge_snoozed(brand)


def sweep(now: datetime | None = None) -> int:
    """Queue a `festival_push` for every brand that has one due. Idempotent:
    the job's dedupe key is (brand, festival), so an hourly sweep over a
    four-hour window still sends each owner one offer per festival."""
    from app.db.session import session_scope
    from app.queue.client import enqueue

    now = now or datetime.now(UTC)
    local = now.astimezone(IST)
    if local.hour not in SWEEP_HOURS:
        return 0
    today = local.date()
    if not sg.upcoming_festivals(today, PUSH_LEAD_DAYS):
        return 0
    queued = 0
    with session_scope() as db:
        rows = db.execute(
            select(Brand, Account).join(Account, Account.id == Brand.account_id)
        ).all()
        todo = []
        for brand, account in rows:
            if not eligible(brand, account):
                continue
            due = due_festival(today, account.locale)
            if due is None or already_pushed(db, brand.id, due):
                continue
            sess = db.scalar(
                select(WaSession)
                .where(WaSession.account_id == account.id)
                .order_by(WaSession.created_at.desc())
                .limit(1)
            )
            wa_id = (sess.wa_id if sess is not None else None) or account.wa_phone
            todo.append((str(account.id), str(brand.id), wa_id, due.key))
    for account_id, brand_id, wa_id, key in todo:
        job = enqueue(
            kind="festival_push",
            payload={"account_id": account_id, "brand_id": brand_id, "wa_id": wa_id},
            dedupe_key=f"festival:{brand_id}:{key}",
        )
        queued += 1 if job else 0
    if queued:
        log.info("festival_sweep", queued=queued, day=today.isoformat())
    return queued


def template_configured() -> bool:
    return bool(settings.wa_template_festival.strip())
