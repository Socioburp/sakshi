"""Brief -> pixels -> WhatsApp.

Three entry points, and the difference between them is the whole cost model:

  generate     charge per image call, generate, composite, upload, send
  recompose    copy-only change: reuse the stored backgrounds, composite again.
               No image call, no charge.
  regenerate   new background, same copy. Charges again.

A carousel is N creatives sharing one `carousel_group_id`.

QUALITY IS NEVER TRADED FOR SPEED. There is one setting -- the best one -- for a
single post and for every slide of a carousel; there are no tiers, no step-downs
and no deadline that changes what is made. Speed is solved by DELIVERY instead:

  * the owner is told the moment work starts;
  * slides are generated in parallel (bounded by IMAGEGEN_CONCURRENCY);
  * a single post is sent the moment it is ready; a carousel is sent as an
    ordered set, or slide by slide as each finishes (CAROUSEL_DELIVERY);
  * if the job is slow the chat says so (SLOW_NOTICE_S) -- it never sends a
    worse picture to hit a time.

Timings are recorded per stage (telemetry/stages.py) and are a metric to watch,
not a limit.

Nothing reaches the owner without passing BOTH gates: the compositor's
deterministic guarantees (compose.LayoutError, checked before the charge by
`layout_gate`) and the background inspection (bggate, enforced by
`_generate_checked`, which regenerates on rejection and fails the slide on
exhaustion rather than deliver a rejected picture).

A slide whose `visual_direction.reference_asset_id` is set skips generation
entirely and composites over the owner's own photograph. That path is free.
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.agent.context import ToolContext
from app.billing import credits
from app.config import settings
from app.creative import (
    bggate,
    claims,
    compose,
    compositeqa,
    dedupe,
    finalgate,
    photoreal,
    photoref,
    product,
    remembered,
    shotplan,
)
from app.creative.brief import CreativeBrief, Slide, check_brand_rules
from app.creative.imagegen import ImageRequest, get_provider, providers
from app.creative.imagegen.base import crop_for, generation_size_for_window
from app.db import repo
from app.db.models import Account, Brand, BrandAsset, Brief, Creative
from app.db.session import session_scope
from app.insights import events
from app.integrations.storage import r2
from app.logging import get_logger

log = get_logger(__name__)

DRAFT_TTL_DAYS = 7


class CreativeFailed(Exception):
    pass


# --------------------------------------------------------------------------- #
# What ONE vendor call may take. OpenAI documents "up to 2 minutes" for a
# gpt-image-2 render at the size and setting this pipeline asks for, and
# nothing here is allowed to buy a cheaper call to beat it.
VENDOR_CEILING_S = 120
# ...and what happens after the last picture lands: the composite render
# (3.2-5.6s measured), the export, the vision look at the finished card (2-5s)
# and the repair ladder when it objects (~10s, ~20s at worst), rounded up.
COMPOSITING_S = 40


def paid_attempts() -> int:
    """How many vendor calls one slide may really make.

    IMAGEGEN_GATE_ATTEMPTS is the ceiling, but IMAGEGEN_GATE_BUDGET_MICROS
    stops the retrying well before it: at the list price of one picture the
    budget pays for three. Derived rather than typed, because the time the
    chat promises the owner is built on it -- move either setting and the
    promise moves with it instead of quietly becoming a lie.
    """
    attempts = max(1, int(settings.imagegen_gate_attempts))
    budget = int(settings.imagegen_gate_budget_micros or 0)
    per_call = providers.price_micros(settings.imagegen_provider)
    if budget and per_call:
        attempts = min(attempts, max(1, budget // per_call))
    return attempts


def working_window(slides: int) -> tuple[int, int]:
    """Seconds this job may honestly take: (a clean first picture, the last
    attempt the budget allows). Slides run IMAGEGEN_CONCURRENCY at a time, so
    a carousel wider than that waits more than once.

    This is the number the chat quotes. It used to be a flat "about 90
    seconds" under a comment admitting it had never been measured, while the
    gate above it allowed three paid calls of up to two minutes each. Ten
    minutes of "still working" later the owner wrote "it's taking too much
    time" -- and they were right, because we had told them ninety seconds.
    """
    lanes = max(1, int(settings.imagegen_concurrency))
    waves = -(-max(1, slides) // lanes)
    return (
        VENDOR_CEILING_S * waves + COMPOSITING_S,
        VENDOR_CEILING_S * paid_attempts() * waves + COMPOSITING_S,
    )


def working_minutes(slides: int) -> tuple[int, int]:
    """The same window in whole minutes: the low end rounded down and the high
    end rounded up, so the range brackets the truth instead of flattering it."""
    low, high = working_window(slides)
    return max(1, low // 60), max(2, -(-high // 60))


_WORKING: dict[str, str] = {
    "hi": "Bana raha hoon… achhi quality mein {low}-{high} minute lagte hain.",
    "kn": "Maadta iddini… {low}-{high} nimisha.",
    "ta": "Panren… {low}-{high} nimidam.",
    "te": "Chestunna… {low}-{high} nimishalu.",
    "mr": "Banavtoy… {low}-{high} minute lagtat.",
    "ml": "Cheyyunnu… {low}-{high} minute.",
    "en": "Making it… it takes {low}-{high} minutes at this quality.",
}


def working_line(locale: str | None, slides: int) -> str:
    lang = ((locale or "en").split("-")[0]).lower()
    low, high = working_minutes(slides)
    return _WORKING.get(lang, _WORKING["en"]).format(low=low, high=high)


# Every notice carries a number that has moved since the last one -- how long
# it has been, and on a carousel how much is done. The owner read the
# identical sentence five times across ten minutes; a line that says nothing
# new is not a progress report, it is noise.
_STILL_WORKING: dict[str, str] = {
    "hi": "Abhi ban raha hai… {mins} minute ho gaye, {done}/{total} taiyaar.",
    "en": "Still working… {mins} minutes in, {done} of {total} ready.",
}
_STILL_WORKING_ONE: dict[str, str] = {
    "hi": "Abhi ban raha hai… {mins} minute ho gaye. Check paas hote hi bhejta hoon.",
    "en": "Still working… {mins} minutes in. It goes to you the moment it passes our check.",
}
# The last word, sent once the job runs past the window it was quoted. After
# this the chat is quiet on purpose: the job either delivers or says it could
# not be made, so the owner hears again either way.
_OVER_TIME: dict[str, str] = {
    "hi": (
        "{high} minute se zyada lag raha hai. Quality kam karke jaldi nahi bhejunga — "
        "nahi bana to credit wapas, aur main bataunga."
    ),
    "en": (
        "This is past the {high} minutes I said. I will not send a worse picture to be "
        "quick — if it cannot be made, the credit comes back and I will tell you."
    ),
}
# Notices go out at 1x and 3x SLOW_NOTICE_S, then one closing line when the
# quoted window runs out. Three at most -- WhatsApp is not a log.
_NOTICE_AT = (1, 3)


class _Delivery:
    """Gets the pictures to the owner, and says so when the job is slow.

    A single post goes the moment it is ready. A carousel follows
    CAROUSEL_DELIVERY: "ordered" holds finished slides and sends the set 1..N
    once the last one lands (the slow notices report "3 of 6 ready" meanwhile);
    "as_ready" sends each slide as it finishes. Either way every carousel image
    is captioned with its place ("3/6").
    """

    def __init__(self, ctx: ToolContext, brief: CreativeBrief, total: int, locale: str) -> None:
        self.ctx, self.brief, self.total = ctx, brief, total
        self.lang = ((locale or "en").split("-")[0]).lower()
        self.sent: dict[int, bool] = {}
        self.ready: dict[int, str] = {}
        self.hold = total > 1 and settings.carousel_delivery == "ordered"
        self._lock = asyncio.Lock()
        self._watch: asyncio.Task | None = None
        # The window the owner was quoted, so the closing notice names the
        # same number the opening line did.
        self.over_s = working_window(total)[1]
        self.over_min = working_minutes(total)[1]

    def caption(self, position: int) -> str:
        if self.total <= 1:
            return self.brief.headline
        return f"{position}/{self.total}" + (f" · {self.brief.headline}" if position == 1 else "")

    async def send(self, position: int, url: str) -> bool:
        """A slide is finished. Sent now, or held for `flush` (ordered carousels)."""
        self.ready[position] = url
        if self.hold:
            return True
        return await self._send(position, url)

    async def flush(self) -> None:
        """Send whatever was held, in slide order. A no-op when nothing was."""
        for position in sorted(self.ready):
            if position not in self.sent:
                await self._send(position, self.ready[position])

    async def _send(self, position: int, url: str) -> bool:
        async with self._lock:  # one message at a time: WhatsApp orders by arrival
            try:
                show = self.ctx.show_video if self.brief.is_reel() else self.ctx.show
                ok = bool(await show(url, caption=self.caption(position)))
            except Exception:  # noqa: BLE001 - the creative exists; the result says it was not shown
                log.exception("slide_send_failed", position=position)
                ok = False
            self.sent[position] = ok
            return ok

    def start(self) -> None:
        if settings.slow_notice_s > 0:
            self._watch = asyncio.create_task(self._notices())

    async def stop(self) -> None:
        if self._watch is not None:
            self._watch.cancel()
            try:
                await self._watch
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def notice_at(self, elapsed: float) -> str:
        """What the owner hears `elapsed` seconds in. Never twice the same
        sentence: the minutes move, and on a carousel so does the count."""
        table = _STILL_WORKING_ONE if self.total <= 1 else _STILL_WORKING
        return table.get(self.lang, table["en"]).format(
            mins=max(1, round(elapsed / 60)), done=len(self.ready), total=self.total
        )

    async def _say(self, line: str, elapsed: float) -> None:
        log.info("slow_notice", elapsed_s=elapsed, done=len(self.ready), total=self.total)
        try:
            await self.ctx.progress(line)
        except Exception:  # noqa: BLE001 - a missed notice must never cost the creative
            log.warning("slow_notice_failed")

    async def _notices(self) -> None:
        elapsed = 0.0
        for mult in _NOTICE_AT:
            await asyncio.sleep(settings.slow_notice_s * mult - elapsed)
            elapsed = settings.slow_notice_s * mult
            await self._say(self.notice_at(elapsed), elapsed)
        # Past the window the owner was quoted. One honest line, and then the
        # chat stays quiet: the job either delivers or says it could not be
        # made, so nobody is left listening to the same sentence for ever.
        await asyncio.sleep(max(0.0, self.over_s - elapsed))
        closing = _OVER_TIME.get(self.lang, _OVER_TIME["en"]).format(high=self.over_min)
        await self._say(closing, self.over_s)


# Credits at or below this get a one-line nudge from the agent. Finding out at
# zero, mid-request, is the worst moment to learn you need a top-up.
LOW_CREDIT_NUDGE = 2


def claim_gate(brief: CreativeBrief, brand: Any) -> dict[str, Any] | None:
    """The industry rulebook, applied like never_say: server-side, before any
    charge. Returns the tool result to hand back, or None when the copy is fine.
    Advice-level hits ride along on the success path (see `claim_notes`)."""
    prefs = getattr(brand, "template_prefs", None) or {}
    hits = claims.check(brief, getattr(brand, "category", None), prefs.get("substantiated"))
    blocked = claims.blocking(hits)
    if not blocked:
        return None
    return {
        "ok": False,
        "reason": "claim_guard",
        "charged": 0,
        "violations": [v.as_dict() for v in blocked],
        "hint": (
            "These claims cannot ship in this industry. Rewrite using the suggested "
            "wording (or something the owner can prove) and call the tool again. If the "
            "owner has a certificate for a phrase, record it with "
            "update_brand(substantiated=[...]) first."
        ),
    }


def in_flight_gate(db: Any, brand_id: uuid.UUID, slides: int) -> dict | None:
    """One creative at a time, per brand. Returns the tool result to hand
    back, or None when nothing of theirs is being made.

    Checked here, beside claim_gate, rather than in the tool: when the owner
    nudges -- "it's taking too much time" -- the model's instinct is to call
    create_creative again, and nothing stopped it. That charged a second
    credit, bought a second set of pictures nobody asked for, and started a
    second stream of "still working" notices on top of the first, which is why
    one slow job read as a bot roaming in circles. The queue's dedupe_key only
    ever covered webhook retries of the same inbound message.

    Rows older than STUCK_AFTER belong to the reaper, not to this guard: a job
    whose worker died must never wedge the brand out of making another.
    """
    row = repo.in_flight_creative(db, brand_id, datetime.now(UTC) - STUCK_AFTER)
    if row is None:
        return None
    low, high = working_minutes(slides)
    return {
        "ok": False,
        "reason": "already_making_one",
        "charged": 0,
        "brief_id": str(row.brief_id),
        "creative_id": str(row.id),
        "eta_minutes": [low, high],
        "hint": (
            "Their picture is ALREADY being made -- nothing was charged and nothing new "
            f"was started. It takes {low}-{high} minutes and it will be sent the moment it "
            "passes our check. Tell them that in ONE short line. Do not offer to try "
            "again and do not call this tool again for this brand until it has arrived; "
            "if they want the words or the picture changed, that is revise_creative or "
            "regenerate_image once it is here."
        ),
    }


def claim_notes(brief: CreativeBrief, brand: Any) -> list[dict[str, str]]:
    """Advice-level claims to soften next time; never a gate."""
    prefs = getattr(brand, "template_prefs", None) or {}
    hits = claims.check(brief, getattr(brand, "category", None), prefs.get("substantiated"))
    return [v.as_dict() for v in hits if v.severity == "advise"]


# What each FIT_JS violation means, in words the agent can act on.
_LAYOUT_WORDS = {
    "overflow": "the copy is too long for the layout",
    "clipped": "a word is wider than the layout, and words are never split",
    "wordbreak": "a word would have to be split across two lines",
    "too_many_lines": "the copy runs to too many lines to read as a post",
    "hierarchy": "the subhead is so long the headline would stop reading as the headline",
    "linegap": "two lines of type would touch",
    "tofu": "the copy uses a character the brand's typefaces cannot set",
    "outside": "the copy runs off the canvas",
    "unsafe": "the copy reaches into the strip the profile grid trims",
    "overlap": "two elements would overlap",
    "logo_clearspace": "the copy crowds the logo",
    "logo_not_loaded": "the logo could not be loaded",
    "photo_window": "the copy would shrink the picture's window below what it was made for",
    "subject_clipped": "the product would be cut by the edge of the picture",
    "subject_unsafe": "the product reaches into the strip the profile grid trims",
}
# The same violations when their subject is the brand's own mark. Shorter copy
# cannot cure any of these, so they are never described as a copy problem.
_MARK_WORDS = {
    "logo_not_loaded": "the brand's logo file could not be loaded",
    "logo_clearspace": "the logo cannot keep its clear space in this layout",
    "too_many_lines": "the brand name is too long to set as the mark, even on two lines",
    "clipped": "a word in the brand name is wider than the layout",
    "wordbreak": "a word in the brand name would have to be split",
    "tofu": "the brand name uses a character the brand's typefaces cannot set",
}
_MARK_DEFAULT = "the brand name or logo cannot be set inside the safe zone"

_COPY_HINT = (
    "Nothing was made and nothing was charged. At a readable size this copy cannot be "
    "set without cropping or overlapping, and we never ship that. Shorten the headline, "
    "subhead or CTA on the slide(s) named -- fewer words, not smaller words -- and call "
    "the tool again. Do not tell the owner about layout; just send the tighter version."
)
_TOFU_HINT = (
    " Where a problem names a character (U+....), remove that character: write it in "
    "plain words instead."
)
_MARK_HINT = (
    "Nothing was made and nothing was charged. The COPY IS FINE -- do not shorten or "
    "rewrite it, that cannot help. What does not fit is the brand's own mark: with no "
    "logo on file the brand NAME is set on every creative, and this one is too long (or "
    "the logo file is broken). Ask the owner for the short name they want on their posts "
    "and save it with update_brand(name=...), or ask them to send their logo; then call "
    "the tool again with the same copy."
)
_FONT_HINT = (
    "Nothing was made and nothing was charged. This is NOT a copy problem: a typeface the "
    "brand is set in did not load on our side. Do not rewrite the copy. Call the tool once "
    "more with the same brief; if it fails again, tell the owner there is a technical "
    "problem on our end and that they have not been charged."
)
# A brand name with an emoji in it is not a LONG name, and the agent told the
# owner "your name is too long" for "Sri Stores ✨" -- so the hint names the
# character instead, and the one action that cures it.
_MARK_TOFU_HINT = (
    "Nothing was made and nothing was charged. The COPY IS FINE -- do not shorten or "
    "rewrite it. With no logo on file the brand NAME is set on every creative, and the name "
    "contains {chars} which the brand's typefaces cannot set. The name is not too long: "
    "remove that character (write it in plain words if it means something) and save the "
    "name with update_brand(name=...), or ask the owner to send their logo; then call the "
    "tool again with the same copy."
)


def _tofu_in_name(problems: list[dict]) -> list[str]:
    """The characters, as 'U+XXXX (x)', that FIT_JS could not set in the brand
    name -- from the 'tofu:brandline:U+....' details of the refused slides."""
    seen: list[str] = []
    for p in problems:
        for detail in p.get("detail") or []:
            kind, _, rest = detail.partition(":")
            subject, _, code = rest.partition(":")
            if kind != "tofu" or subject != "brandline" or not code.startswith("U+"):
                continue
            try:
                ch = chr(int(code[2:], 16))
            except ValueError:
                continue
            shown = f"{code} ({ch!r})" if ch.isprintable() else code
            if shown not in seen:
                seen.append(shown)
    return seen


def _about_the_mark(violation: str) -> bool:
    """True when the thing that does not fit is the brand's name or logo itself,
    not the copy around it. "overlap:headline+logo" and
    "logo_clearspace:subhead" are the COPY crowding the mark, and stay copy."""
    kind, _, subject = violation.partition(":")
    if kind == "logo_not_loaded":
        return True
    if kind in ("overlap", "overflow", "hierarchy"):
        return False
    if kind == "logo_clearspace":
        return subject == "edge"
    return subject.split(":", 1)[0] in ("brandline", "logo")


# The boxes FIT_JS checks for overflow. A violation on one of these names no
# element: it says something INSIDE spilled, and which thing that was is what
# the element-level violations say.
_CONTAINERS = frozenset({"content", "panel", "band", "card"})


def _knock_on(violation: str) -> bool:
    """A container spilling because of what is in it. It follows the element
    that spilled and never decides on its own: a one-word shop name too wide
    for split_card's panel produced 'overflow:panel' beside the brandline's
    own violations, and that one word made the slide a copy problem -- the
    agent was told to shorten a headline that had nothing to do with it."""
    kind, _, subject = violation.partition(":")
    return kind in ("overflow", "outside") and subject in _CONTAINERS


def _blame(violations: list[str]) -> tuple[bool, list[str]]:
    """(the mark alone is at fault, the violations worth describing)."""
    own = [v for v in violations if not _knock_on(v)]
    mark_only = bool(own) and all(_about_the_mark(v) for v in own)
    return mark_only, own if mark_only else violations


async def layout_gate(
    brief: CreativeBrief,
    units: list[Slide],
    brand_snapshot,
    reports: dict[int, dict] | None = None,
) -> dict | None:
    """The deterministic guarantees, checked before any money moves.

    Returns the tool result to hand back when a slide cannot be set, or None.
    There is no degraded render to fall back to. WHAT has to change is part of
    the answer: a brand whose 44-character name was set as its mark used to be
    told "shorten the headline" on every request, for ever, and a typeface that
    failed to load surfaced as a bare tool error -- the agent rewrote good copy
    in a loop and the owner got nothing.

    `reports`, when given, receives each slide's layout report by position:
    the photo window the picture must be generated for, and where the type
    will sit, both known here before a rupee is spent.
    """
    results = await asyncio.gather(
        *(compose.check_layout(brief, s, brand_snapshot) for s in units),
        return_exceptions=True,
    )
    problems, faces = [], []
    for slide, res in zip(units, results, strict=True):
        if isinstance(res, dict) and reports is not None:
            reports[slide.position] = res
        if isinstance(res, compose.LayoutError):
            mark_only, described = _blame(res.violations)
            words = []
            for v in described:
                kind = v.split(":", 1)[0]
                if _about_the_mark(v):
                    words.append(_MARK_WORDS.get(kind, _MARK_DEFAULT))
                else:
                    words.append(_LAYOUT_WORDS.get(kind, kind))
            problems.append(
                {
                    "slide": slide.position,
                    "headline": slide.headline,
                    "about": "brand_mark" if mark_only else "copy",
                    "problems": list(dict.fromkeys(words)),
                    "detail": res.violations[:6],
                }
            )
        elif isinstance(res, compose.BrandFontUnavailable):
            faces.append({"slide": slide.position, "detail": str(res)})
        elif isinstance(res, BaseException):
            raise res
    if faces:
        log.error("layout_gate_fonts_unavailable", slides=faces)
        return {
            "ok": False,
            "reason": "brand_font_unavailable",
            "charged": 0,
            "slides": faces,
            "hint": _FONT_HINT,
        }
    if not problems:
        return None
    log.warning("layout_gate_refused", slides=[p["slide"] for p in problems])
    if all(p["about"] == "brand_mark" for p in problems):
        tofu = _tofu_in_name(problems)
        return {
            "ok": False,
            "reason": "brand_mark_does_not_fit",
            "charged": 0,
            "slides": problems,
            "hint": _MARK_TOFU_HINT.format(chars=", ".join(tofu)) if tofu else _MARK_HINT,
        }
    hint = _COPY_HINT
    if any(d.startswith("tofu:") for p in problems for d in p["detail"]):
        hint += _TOFU_HINT
    if any(p["about"] == "brand_mark" for p in problems):
        hint += " On a slide marked brand_mark the copy is fine; see its problems."
    return {
        "ok": False,
        "reason": "copy_does_not_fit",
        "charged": 0,
        "slides": problems,
        "hint": hint,
    }


async def generate(
    ctx: ToolContext,
    brief: CreativeBrief,
    *,
    reuse: dict[int, tuple[str, str]] | None = None,
    parent: uuid.UUID | None = None,
) -> dict:
    """Generate every slide of a brief.

    `reuse` maps slide position -> (background_key, background_url) for slides
    whose picture is being KEPT. That is how regenerating one slide of a
    carousel charges for one slide: the others are re-composited over the
    background they already have, exactly like a copy revision.

    `parent` is the brief this one revises. A picture revision used to save a
    fresh root, so after one "change the picture" nobody could say which
    version the owner was looking at; with the parent the new brief is version
    N+1 of the same creative, exactly as a copy revision is.
    """
    units = brief.units()
    group_id = uuid.uuid4()
    reuse = dict(reuse or {})

    with session_scope() as db:
        brand = db.get(Brand, ctx.brand_id)
        if brand is None:
            return {"ok": False, "reason": "unknown_brand"}

        # Before anything else, including the browser work below: this brand
        # is allowed one creative in flight. A revision comes through here
        # too, and is guarded the same -- it charges, generates and starts its
        # own notice loop exactly as a fresh creative does, so a "make it
        # again" on top of a running job is the same double spend.
        busy = in_flight_gate(db, ctx.brand_id, len(units))
        if busy:
            return busy

        violations = check_brand_rules(brief, brand)
        if violations:
            return {
                "ok": False,
                "reason": "never_say_violation",
                "violations": violations,
                "hint": "Rewrite the copy without those phrases and call the tool again.",
            }
        blocked = claim_gate(brief, brand)
        if blocked:
            return blocked

        # Real photograph first. Any slide the agent left unreferenced gets the
        # owner's own photo when one genuinely matches -- decided here, before
        # the charge, so the free lane is actually free.
        resolved = _resolve_photos(db, ctx.brand_id, brief, units)
        assets = _load_assets(db, ctx.brand_id, units, resolved)
        brand_snapshot = _snapshot(db, brand)
        # The layout their approved posts share -- read BEFORE this brief is
        # stored, so a creative is never evidence for its own "usual".
        usual_template = None
        try:
            from app.creative import grid

            fp = grid.fingerprint(db, ctx.brand_id)
            usual_template = fp.dominant(fp.templates) if fp.enough else None
        except Exception:  # noqa: BLE001 - a missing signature costs one remembered fact
            log.warning("grid_fingerprint_failed", brand_id=str(ctx.brand_id))

    # The owner's photographs are read ONCE, here, before a layout is chosen
    # and before the charge: upright pixels, the cut-out where the photo is a
    # product, and where the subject is either way. A photo's layout is
    # decided per slide from what is in it -- the words move off a product
    # that keeps its cut, and off the subject of a photo whose cut is refused
    # -- so the render, the stored brief, a later free revision and the
    # taste/grid history all agree. It used to be decided for the whole brief
    # before the cut was even attempted.
    photos = await _plan_photos(units, assets, resolved)
    switches = _choose_photo_layouts(brief, units, photos)
    units = brief.units()

    # Prove the copy can be set -- no overlap, no crop, inside the safe zone,
    # the mark clear -- BEFORE the brief is stored or a credit is charged. The
    # layout does not depend on the picture, so there is no reason to buy one
    # first and find out afterwards.
    layouts: dict[int, dict] = {}
    unfit = await layout_gate(brief, units, brand_snapshot, layouts)
    if unfit:
        return unfit
    # A photo with too few pixels for its window is never enlarged to fill
    # it: a layout with a smaller window takes it, or the job is refused
    # with the ask that gets a better file -- before anything is charged.
    too_small = await _photo_resolution_gate(
        brief, units, photos, layouts, brand_snapshot, switches
    )
    if too_small:
        return too_small
    # And the owner's product is never stood in the frame as a thumbnail: the
    # copy that leaves it no room moves the slide to a layout with a window,
    # or the job is refused asking for shorter copy -- again before the charge.
    thumbnail = await _product_size_gate(brief, units, photos, layouts, brand_snapshot, switches)
    if thumbnail:
        return thumbnail
    units = brief.units()

    with session_scope() as db:
        # Asked again in the transaction that charges. The gate above runs
        # before several seconds of layout and photo work, so two requests
        # that arrived together could both have passed it; here the loser
        # still walks away before a credit moves.
        busy = in_flight_gate(db, ctx.brand_id, len(units))
        if busy:
            return busy

        billable_positions = {
            u.position for u in units if not resolved.get(u.position) and u.position not in reuse
        }
        billable = len(billable_positions)

        # Re-loaded in the session that saves, so the SUPERSEDED stamp and
        # the child row land in one transaction.
        parent_row = db.get(Brief, parent) if parent is not None else None
        brief_row = repo.save_brief(
            db,
            account_id=ctx.account_id,
            brand_id=ctx.brand_id,
            payload=brief.model_dump(mode="json"),
            source_message_id=ctx.message_id,
            parent=parent_row,
        )
        brief_id = brief_row.id
        version, root_brief_id = brief_row.version, brief_row.root_brief_id
        revision_no = repo.revision_no(db, brief_id)
        # A revision is the caller's 'regenerate' event, not a second
        # 'created': profile.taste divides approvals by created rows, and a
        # picture change used to count as one more creative the owner never
        # approved.
        if parent_row is None:
            events.record(
                db,
                kind="created",
                account_id=ctx.account_id,
                brand_id=ctx.brand_id,
                brief_id=brief_id,
                meta={
                    **events.facts_of(brief_row.payload or {}, version=version),
                    "photo_slides": sorted(resolved),
                    "reused_slides": sorted(reuse),
                },
            )

        w, h = brief.pixel_size()
        creative_ids: list[uuid.UUID] = []
        for slide in units:
            c = Creative(
                brief_id=brief_id,
                brand_id=ctx.brand_id,
                template=brief.template_for(slide),
                aspect=brief.format.aspect_ratio,
                width=w,
                height=h,
                carousel_group_id=group_id if brief.is_carousel() else None,
                slide_position=slide.position,
                background_key=reuse.get(slide.position, (None, None))[0],
                background_url=reuse.get(slide.position, (None, None))[1],
                billed=slide.position in billable_positions,
                status="generating",
                expires_at=datetime.now(UTC) + timedelta(days=DRAFT_TTL_DAYS),
            )
            db.add(c)
            db.flush()
            creative_ids.append(c.id)

        if billable:
            try:
                credits.charge(
                    db,
                    account_id=ctx.account_id,
                    action="generate_creative",
                    units=billable,
                    idempotency_key=f"creative:{group_id}",
                    ref_type="brief",
                    ref_id=str(brief_id),
                )
            except credits.InsufficientCredits as exc:
                for cid in creative_ids:
                    row = db.get(Creative, cid)
                    row.status, row.error = "failed", "insufficient_credits"
                return {
                    "ok": False,
                    "reason": "insufficient_credits",
                    "balance": exc.balance,
                    "needed": exc.needed,
                    "hint": "Tell the owner they are out of credits and offer a top-up.",
                }
        locale = (db.get(Account, ctx.account_id).locale or "en") if ctx.account_id else "en"

    # "Making it..." goes out now -- after validation and the charge, before the
    # 5-40 seconds of work. On WhatsApp that silence reads as "it broke", and
    # the owner types again, which queues a second charged request. It is sent
    # here rather than by the model so it is never skipped, and it is allowed
    # to fail: a missed progress line must never cost the creative.
    try:
        await ctx.say(working_line(locale, len(units)))
    except Exception:  # noqa: BLE001
        log.warning("progress_line_failed", account_id=str(ctx.account_id))

    # Slides run concurrently. gather with return_exceptions so one bad slide
    # does not discard the ones that already succeeded.
    # Every slide gets its own rung of the shot ladder and its own seed before
    # anything is dispatched. Without this the whole carousel shared one lens,
    # one distance and one angle, and came back as one picture six times.
    shot_plan = shotplan.apply(brief) if brief.is_carousel() else []
    if shot_plan:
        log.info("shot_plan", carousel_group_id=str(group_id), plan=shot_plan)

    # Slides run concurrently but share a duplicate register, so a slide that
    # lands on a picture an earlier slide already produced is caught and
    # regenerated instead of shipping. The register also carries the semaphore
    # that bounds how many vendor calls are in flight at once.
    register = dedupe_register()
    delivery = _Delivery(ctx, brief, len(units), locale)
    delivery.start()
    try:
        results = await asyncio.gather(
            *(
                _build_one(
                    ctx,
                    brief,
                    slide,
                    cid,
                    brand_snapshot,
                    assets,
                    resolved,
                    reuse,
                    register,
                    delivery,
                    layouts.get(slide.position),
                    photos.get(slide.position),
                )
                for slide, cid in zip(units, creative_ids, strict=True)
            ),
            return_exceptions=True,
        )
    finally:
        await delivery.stop()
    # Ordered carousels go out now, 1..N. Slides that failed are simply absent.
    with ctx.trace.stage("deliver_set"):
        await delivery.flush()
    if register["hashes"]:
        log.info(
            "carousel_variety",
            carousel_group_id=str(group_id),
            **dedupe.report(register["hashes"]),
        )

    ok_urls: list[str] = []
    failures: list[str] = []
    failed_billable = 0
    for slide, cid, res in zip(units, creative_ids, results, strict=True):
        if isinstance(res, BaseException):
            log.error(
                "slide_failed", creative_id=str(cid), position=slide.position, error=str(res)[:300]
            )
            # A slide that exhausted the gate still cost real money at the vendor.
            _mark_failed(cid, str(res), cost_micros=getattr(res, "cost_micros", 0))
            failures.append(f"slide {slide.position}: {res}")
            # A failed slide is refunded only if it was paid for. A free photo
            # slide that failed to fetch was never charged, so refunding it
            # would hand back a credit the owner did not spend.
            if slide.position in billable_positions:
                failed_billable += 1
        else:
            ok_urls.append(res)

    # What the final check would have the agent say. A slide it refused is not
    # "try again": on the owner's own photograph nothing would change, and on a
    # generated one the corrected retry has already been bought and refused.
    # The hint reached the agent only on revisions, because this path flattened
    # every failure into "generation_failed" and dropped it.
    refused = [r for r in results if isinstance(r, CompositeRejected)]
    quality_hint = next((r.hint for r in refused if r.hint), "")

    if failures and not ok_urls:
        _refund(ctx, group_id, billable, "creative_failed")
        if quality_hint and len(refused) == len(failures):
            return {
                "ok": False,
                "reason": "composite_quality",
                "errors": failures[:3],
                "hint": quality_hint,
            }
        return {"ok": False, "reason": "generation_failed", "errors": failures[:3]}

    if failed_billable:
        # Partial carousel: refund only the paid slides that did not ship.
        _refund(ctx, group_id, failed_billable, "partial_carousel")

    # Already sent -- as each finished, or as an ordered set (see _Delivery).
    delivered = bool(ok_urls) and len(delivery.sent) == len(ok_urls) and all(delivery.sent.values())
    if not delivered:
        log.error("creative_not_delivered", account_id=str(ctx.account_id), urls=len(ok_urls))
    with session_scope() as db:
        balance = db.get(Account, ctx.account_id).credits_balance
    _schedule_daily_nudge(ctx)
    charged = billable - failed_billable
    out = {
        "ok": True,
        "brief_id": str(brief_id),
        "creative_ids": [str(c) for c in creative_ids],
        "carousel_group_id": str(group_id) if brief.is_carousel() else None,
        "slides_ok": len(ok_urls),
        "slides_failed": len(failures),
        "image_urls": ok_urls,
        "shown_to_user": delivered,
        "credits_charged": charged,
        "credits_left": balance,
        "version": version,
        "revision_no": revision_no,
        "root_brief_id": str(root_brief_id) if root_brief_id else str(brief_id),
        "note": "The owner can see it now. Ask if they want changes; keep it to one line.",
    }
    # What was KNOWN about this brand and used here, for the agent to say out
    # loud. Derived from what the pipeline actually did -- never inferred.
    shipped = {
        s.position for s, r in zip(units, results, strict=True) if not isinstance(r, BaseException)
    }
    known = remembered.build(
        brief=brief,
        grounding=getattr(ctx, "grounding", None),
        photo_labels={
            pos: assets[ref].label
            for pos, ref in sorted(resolved.items())
            if pos in shipped and ref in assets
        },
        usual_template=usual_template,
        seeded_family=next(
            iter((getattr(brand_snapshot, "template_prefs", None) or {}).get("family") or []), None
        ),
        palette=dict(getattr(brand_snapshot, "palette", {}) or {}),
        generated=bool(shipped & billable_positions),
    )
    if known:
        out["remembered"] = known
        out["remembered_hint"] = remembered.HINT
    if failures:
        out["failed_slides"] = failures[:6]
        out["note"] = (
            f"{len(ok_urls)} of {len(units)} slides were made and sent; the rest could not be made "
            "to our standard and were refunded (see failed_slides). We never send a picture "
            "that failed the check. Tell the owner plainly, in one line, which slide is missing "
            "and offer to try that slide again with regenerate_image."
        )
        if quality_hint:
            out["hint"] = quality_hint
            out["note"] = (
                f"{len(ok_urls)} of {len(units)} slides were made and sent; the rest did not pass "
                "the last look at the finished card and were refunded (see failed_slides). We "
                "never send a card that failed the check. Tell the owner plainly, in one line, "
                "which slide is missing, and offer what `hint` says instead of another "
                "generation."
            )
    if brief.is_story():
        out["format"] = "story"
        out["story_note"] = (
            "Made full-screen (9:16) as they chose: it fits an Instagram Story and a "
            "WhatsApp Status as it is -- they can forward it to their Status straight from "
            "this chat. publish_to_instagram posts it as a Story. It does not go on their "
            "grid. Say this once, in one line."
        )
    if brief.is_reel():
        out["format"] = "reel"
        out["reel_note"] = (
            "Sent as a 7-second video. It is silent: if they post it themselves, one line "
            "suggests adding a trending audio in the Instagram app; publish_to_instagram "
            "posts it as a Reel with the still as its cover. It also works as a WhatsApp "
            "Status. Say this once, briefly."
        )
    if advice := claim_notes(brief, brand_snapshot):
        out["claim_notes"] = advice
        out["claim_hint"] = (
            "Shipped, but these phrases invite a complaint in this industry; use the "
            "suggested wording next time. Do not mention this to the owner unprompted."
        )
    if switches:
        out["template"] = brief.template_id
        out["template_switches"] = {
            str(pos): {"from": was, "to": now, "why": why}
            for pos, (was, now, why) in switches.items()
        }
        out["template_note"] = (
            "Their own photo is in this creative, so the layout of the slide(s) named in "
            "template_switches was changed to keep the words off the subject, to show a "
            "small photo without enlarging it, or to give their product room to be seen. "
            "Do not mention this unless they ask."
        )
    if not delivered:
        # Never tell the model the owner has seen something they have not. The
        # creative exists and is paid for; the honest move is to say so.
        out["note"] = (
            "The creative was made but WhatsApp did NOT deliver it (send failed or the "
            "24h window is closed). Tell the owner in one line that it is ready and "
            "will be sent as soon as they reply. Do not describe it."
        )
    if balance <= LOW_CREDIT_NUDGE:
        out["credits_note"] = (
            f"The owner has {balance} credit(s) left. Mention it in one short line and "
            "offer a top-up link -- do not lecture."
        )
    return out


_UNSUPPORTED_HINT = (
    "Nothing was made. A free revision can change only what the brief expresses: "
    "headline, subhead, cta, caption (body, hashtags, language), alt_text, template_id, "
    "and on a carousel the slides. Nothing renders a badge, a price tag, a logo size or a "
    "text colour, so do not promise one. If the owner wants the PICTURE different, that is "
    "regenerate_image (1 credit). Otherwise tell them in one line what can change and ask "
    "which they want."
)
_NOTHING_CHANGED_HINT = (
    "Nothing was made: the changes leave the creative exactly as it is (same words, same "
    "layout, same caption). Re-read what the owner asked for and send the fields that "
    "actually differ -- or, if their wish is about the picture, use regenerate_image."
)


async def recompose(
    ctx: ToolContext, *, brief_id: uuid.UUID, changes: dict, owner_request: str | None = None
) -> dict:
    """Copy-only revision. Reuses every stored background: no image call, no charge.

    `owner_request` is the change in the owner's words, restated by the agent.
    It is stored on the 'revise' event beside the diff of what was actually
    changed, and remembered for the brand, so "what did they ask for and did
    we do it" can be answered from the database later.
    """
    # Refused before anything is stored: a wish the brief cannot express, or a
    # "change" that changes nothing. Either used to return ok:True with an
    # identical picture, and the owner asked again.
    unsupported = unsupported_changes(changes)
    if unsupported:
        return {
            "ok": False,
            "reason": "unsupported_change",
            "unsupported": unsupported,
            "hint": _UNSUPPORTED_HINT,
        }
    # The new copy has to be settable before anything is stored. Done in its own
    # short session so the browser work does not hold a pooled connection.
    with session_scope() as db:
        parent, brand = db.get(Brief, brief_id), db.get(Brand, ctx.brand_id)
        if parent is None:
            return {"ok": False, "reason": "unknown_brief"}
        if brand is None:
            return {"ok": False, "reason": "unknown_brand"}
        try:
            draft = CreativeBrief.model_validate(_merge(parent.payload, changes))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": "invalid_changes", "error": str(exc)[:400]}
        diff = payload_diff(parent.payload or {}, draft.model_dump(mode="json"))
        if not diff:
            return {"ok": False, "reason": "nothing_changed", "hint": _NOTHING_CHANGED_HINT}
        snap = _snapshot(db, brand)
        # What the stored pictures ARE, so the guard below can tell whether they
        # still suit the shape and layout the revision asks for.
        before = {
            "format": dict(parent.payload.get("format") or {}),
            "slides": _stored_pictures(repo.creatives_for_brief(db, brief_id)),
        }
    layouts: dict[int, dict] = {}
    unfit = await layout_gate(draft, draft.units(), snap, layouts)
    if unfit:
        return unfit
    # A revision never reuses a picture the new version cannot show.
    refused, rebuilt = await _revision_guard(draft, before, snap, layouts)
    if refused:
        return refused

    with session_scope() as db:
        parent = db.get(Brief, brief_id)
        if parent is None:
            return {"ok": False, "reason": "unknown_brief"}
        brand = db.get(Brand, ctx.brand_id)
        prev = repo.creatives_for_brief(db, brief_id)
        if not prev or not all(c.background_key for c in prev):
            return {"ok": False, "reason": "no_background_to_reuse"}
        now = datetime.now(UTC)
        if any(c.expires_at and c.expires_at <= now for c in prev):
            # drafts/ objects are deleted by the R2 lifecycle rule after
            # DRAFT_TTL_DAYS; composing over a key that is gone would strand the
            # new rows in "composing". Say so, and point at the paid path.
            return {
                "ok": False,
                "reason": "background_expired",
                "hint": (
                    f"Drafts are kept for {DRAFT_TTL_DAYS} days and this one is older. "
                    "Use regenerate_image (1 credit) or create_creative."
                ),
            }

        brief = draft
        violations = check_brand_rules(brief, brand)
        if violations:
            return {"ok": False, "reason": "never_say_violation", "violations": violations}
        blocked = claim_gate(brief, brand)
        if blocked:
            return blocked

        units = brief.units()
        if len(units) != len(prev):
            return {
                "ok": False,
                "reason": "slide_count_changed",
                "hint": "Adding or removing slides needs create_creative, not a revision.",
            }

        new_brief = repo.save_brief(
            db,
            account_id=ctx.account_id,
            brand_id=ctx.brand_id,
            payload=brief.model_dump(mode="json"),
            source_message_id=ctx.message_id,
            parent=parent,
        )
        new_brief_id = new_brief.id
        version, root_brief_id = new_brief.version, new_brief.root_brief_id
        revision_no = repo.revision_no(db, new_brief_id)
        events.record(
            db,
            kind="revise",
            account_id=ctx.account_id,
            brand_id=ctx.brand_id,
            brief_id=parent.id,
            meta={
                **events.facts_of(parent.payload or {}, version=parent.version),
                "changed": sorted(changes),
                "owner_request": owner_request,
                "request_text": repo.request_text(db, ctx.message_id),
                "revision_no": revision_no,
                "new_brief_id": str(new_brief_id),
                "root_brief_id": str(root_brief_id),
                "diff": diff,
            },
        )
        _remember_change(
            db,
            brand_id=ctx.brand_id,
            kind="feedback",
            owner_request=owner_request,
            fields=sorted(diff),
            brief_id=new_brief_id,
        )
        group_id = uuid.uuid4() if brief.is_carousel() else None
        w, h = brief.pixel_size()

        pairs: list[tuple[Slide, uuid.UUID, str]] = []
        for slide, old in zip(units, sorted(prev, key=lambda c: c.slide_position), strict=True):
            c = Creative(
                brief_id=new_brief_id,
                brand_id=ctx.brand_id,
                template=brief.template_for(slide),
                aspect=brief.format.aspect_ratio,
                width=w,
                height=h,
                carousel_group_id=group_id,
                slide_position=slide.position,
                background_key=old.background_key,
                background_url=old.background_url,
                status="composing",
                expires_at=datetime.now(UTC) + timedelta(days=DRAFT_TTL_DAYS),
            )
            db.add(c)
            db.flush()
            if slide.position in rebuilt:
                # The product stood in a new window for free (see _revision_guard).
                c.background_key = _store_rebuilt(ctx, c.id, rebuilt[slide.position])
                c.background_url = r2.public_url(c.background_key)
            pairs.append((slide, c.id, c.background_key, old.imagegen_provider or ""))
        brand_snapshot = _snapshot(db, brand)

    results = await asyncio.gather(
        *(
            _recompose_one(
                ctx,
                brief,
                slide,
                cid,
                key,
                brand_snapshot,
                lane not in PHOTO_LANES or slide.position in rebuilt,
                rebuilt.get(slide.position),
            )
            for slide, cid, key, lane in pairs
        ),
        return_exceptions=True,
    )
    refused = _contain_revision(pairs, results)
    if refused:
        return refused
    urls = [str(u) for u in results]
    delivered = await _show(ctx, brief, urls)
    out = {
        "ok": True,
        "brief_id": str(new_brief_id),
        "creative_ids": [str(cid) for _, cid, _, _ in pairs],
        "image_urls": urls,
        "shown_to_user": delivered,
        "credits_charged": 0,
        "applied": diff,
        "version": version,
        "revision_no": revision_no,
        "root_brief_id": str(root_brief_id),
        "note": (
            "Copy-only revision: same backgrounds reused, nothing charged. `applied` is "
            "exactly what changed -- confirm it to the owner in one line."
        ),
    }
    if not delivered:
        # The same honesty generate() keeps: a revision whose send was refused
        # (window closed, provider rejected it) used to be reported as shown,
        # and the owner was told "here is the new version" about a picture
        # that never arrived. The version exists and is free; say that.
        out["note"] = (
            "The revision was made but WhatsApp did NOT deliver it (send failed or the "
            "24h window is closed). Tell the owner in one line that it is ready and "
            "will be sent as soon as they reply. Do not describe it."
        )
    return out


async def regenerate_image(
    ctx: ToolContext,
    *,
    brief_id: uuid.UUID,
    new_prompt: str | None,
    slide_position: int | None = None,
    owner_request: str | None = None,
    template_id: str | None = None,
) -> dict:
    """A new picture for an existing creative, 1 credit.

    `template_id` moves the slide to another layout at the same time, which is
    the ONLY way a generated picture can follow the owner into a new layout: a
    free revision that changes the layout is refused by _revision_guard,
    because the stored picture was made for the window the old layout showed.
    _LAYOUT_PICTURE_HINT sent the agent here for exactly that and the
    parameter did not exist, so the agent either looped or regenerated for the
    old layout again and the owner ended up where they started.
    """
    with session_scope() as db:
        parent = db.get(Brief, brief_id)
        if parent is None:
            return {"ok": False, "reason": "unknown_brief"}
        # A deep copy: the slide dicts below are rewritten in place, and the
        # parent's payload is what the diff on the event is measured against.
        parent_payload = copy.deepcopy(parent.payload)
        parent_version = parent.version
        payload = copy.deepcopy(parent.payload)
        rows = repo.creatives_for_brief(db, brief_id)
        own = _owner_photo_slides(rows, slide_position)
        photo_slides = {r["slide"] for r in _owner_photo_slides(rows, None)}
        every_slide_is_a_photo = bool(rows) and photo_slides >= {c.slide_position for c in rows}
    # The refusal below is the whole truth only when there is nothing else to
    # redo. Asked to redo a six-slide carousel whose slide 1 is the owner's
    # shopfront, it refused all six -- so "the pictures all look the same,
    # make them different" regenerated nothing, and the hint sent the agent to
    # create_creative, which charges for six and throws away the five the
    # owner was happy with. A mixed carousel keeps the photographs and redoes
    # the generated slides, which is what was asked for. Named explicitly, a
    # photo slide is still refused: there the owner asked for THAT slide, and
    # regenerating it really would hand back the same photo.
    mixed = own and slide_position is None and not every_slide_is_a_photo
    keep_photos = sorted(photo_slides) if mixed else []
    if own and not keep_photos:
        # "Change the picture" on a slide that shows the owner's own photograph
        # would run the same free lane and hand back the same photo, and
        # spend a revision doing it. Said plainly instead.
        return {
            "ok": False,
            "reason": "picture_is_the_owners_photo",
            "charged": 0,
            "slides": own,
            "hint": (
                "The slide(s) named show the owner's OWN photograph, not a generated picture, "
                "so regenerating would return the same photo. Ask the owner what they want: "
                "a different photo of theirs (create_creative with that reference_asset_id), "
                "or a generated picture (create_creative with no reference_asset_id and a "
                "headline that does not name the photographed product). Nothing was charged."
            ),
        }

    if new_prompt:
        targets = payload.get("slides") or []
        if slide_position and targets:
            targets = [s for s in targets if s["position"] == slide_position]
        for s in targets:
            # A carousel's pictures come from the slides, never from the top-level
            # direction -- writing there used to change nothing and charge for it.
            s["visual_direction"] = {**s["visual_direction"], "prompt": new_prompt}
            s["visual_direction"].pop("seed", None)
        if not payload.get("slides"):
            payload["visual_direction"] = {**payload["visual_direction"], "prompt": new_prompt}
            payload["visual_direction"].pop("seed", None)

    if template_id:
        if template_id not in compose.TEMPLATES:
            # compose falls back to the default for a name it does not know,
            # so an unknown one would silently charge for the layout the owner
            # already had.
            return {
                "ok": False,
                "reason": "unknown_template",
                "charged": 0,
                "hint": f"The layouts are {', '.join(sorted(compose.TEMPLATES))}.",
            }
        if payload.get("slides") and slide_position:
            for s in payload["slides"]:
                if s["position"] == slide_position:
                    s["template_id"] = template_id
        else:
            # The whole creative moves: the brief's layout is the new one and
            # no slide keeps an override of the old one.
            payload["template_id"] = template_id
            for s in payload.get("slides") or []:
                s["template_id"] = None

    try:
        brief = CreativeBrief.model_validate(payload)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "invalid_visual_direction", "error": str(exc)[:400]}
    # Measured now, before generate() runs shotplan.apply on this very object:
    # that gives every seedless slide a seed in place, so a diff taken after
    # the work said all three pictures of a carousel changed when the owner
    # asked for one. The event is what "the product gets smarter" learns from,
    # so it names exactly the slides the owner sent back and nothing else.
    diff = payload_diff(parent_payload, brief.model_dump(mode="json"))

    reuse: dict[int, tuple[str, str]] = {}
    if brief.is_carousel():
        positions = {u.position for u in brief.units()}
        if slide_position is not None and slide_position not in positions:
            return {
                "ok": False,
                "reason": "unknown_slide",
                "hint": f"This carousel has slides {sorted(positions)}.",
            }
        # Keep every other slide's picture. Without this, redoing slide 3 of
        # six re-bought all six -- and replaced five the owner already liked.
        # With no slide named it is the owner's own photographs that are kept:
        # regenerating one returns the same photo, so only the generated
        # slides are redone and only they are charged.
        kept = positions - {slide_position} if slide_position is not None else set(keep_photos)
        if kept:
            now = datetime.now(UTC)
            with session_scope() as db:
                prev = [
                    c for c in repo.creatives_for_brief(db, brief_id) if c.slide_position in kept
                ]
                if any(c.expires_at and c.expires_at <= now for c in prev):
                    return {
                        "ok": False,
                        "reason": "background_expired",
                        "hint": (
                            f"Drafts are kept for {DRAFT_TTL_DAYS} days; the other slides' "
                            "pictures are gone, so one slide cannot be redone alone. Call "
                            f"regenerate_image without slide_position ({len(positions)} "
                            "credits) or create_creative."
                        ),
                    }
                for c in prev:
                    if c.background_key:
                        reuse[c.slide_position] = (
                            c.background_key,
                            c.background_url or "",
                            c.imagegen_provider or "",
                        )
    result = await generate(ctx, brief, reuse=reuse, parent=brief_id)
    # Recorded after the work so the event names the version it produced.
    # The vote stands either way: the owner disliked the picture whether or
    # not a new one could be made.
    new_brief_id = result.get("brief_id") if result.get("ok") else None
    with session_scope() as db:
        events.record(
            db,
            kind="regenerate",
            account_id=ctx.account_id,
            brand_id=ctx.brand_id,
            brief_id=brief_id,
            meta={
                **events.facts_of(parent_payload, version=parent_version),
                "slide_position": slide_position,
                "new_prompt": new_prompt,
                "owner_request": owner_request,
                "request_text": repo.request_text(db, ctx.message_id),
                "revision_no": result.get("revision_no"),
                "new_brief_id": new_brief_id,
                "root_brief_id": result.get("root_brief_id"),
                "diff": diff,
                "ok": bool(result.get("ok")),
                "reason": result.get("reason"),
            },
        )
        if new_brief_id:
            _remember_change(
                db,
                brand_id=ctx.brand_id,
                kind="rejection",
                owner_request=owner_request,
                fields=[f"slide {slide_position} picture" if slide_position else "picture"],
                brief_id=new_brief_id,
            )
    if keep_photos and result.get("ok"):
        # Said out loud rather than passed over: the owner asked for every
        # picture and some of them are their own, which no amount of
        # regenerating changes.
        result["kept_owner_photos"] = keep_photos
        result["kept_owner_photos_hint"] = (
            f"Slide(s) {keep_photos} show the owner's OWN photograph, so they were kept as "
            "they were and not charged -- regenerating one returns the same photo. Tell them "
            "in one line which slides are new. If they want those slides different too, they "
            "need to send another photo (create_creative with that reference_asset_id)."
        )
    return result


# The retention loop. After a creative, the owner gets tomorrow's idea at
# 10:00 IST -- one line, three buttons -- if they are still inside the 24h
# window (send.py suppresses it otherwise) and have not switched it off.
NUDGE_HOUR_IST = 10


def _schedule_daily_nudge(ctx: ToolContext) -> None:
    if not ctx.wa_id:
        return  # nowhere to send it
    try:
        from zoneinfo import ZoneInfo

        from app.queue.client import enqueue

        ist = ZoneInfo("Asia/Kolkata")
        now_ist = datetime.now(ist)
        when = now_ist.replace(hour=NUDGE_HOUR_IST, minute=0, second=0, microsecond=0)
        if when - now_ist < timedelta(hours=1):
            when += timedelta(days=1)  # today's slot is gone; tomorrow's is inside 24h
        enqueue(
            kind="daily_suggestion",
            payload={
                "account_id": str(ctx.account_id),
                "brand_id": str(ctx.brand_id),
                "wa_id": ctx.wa_id,
            },
            dedupe_key=f"daily:{ctx.brand_id}:{when.date().isoformat()}",
            scheduled_for=when.astimezone(UTC),
        )
    except Exception:  # noqa: BLE001 - a missed nudge must never cost the creative
        log.warning("daily_nudge_schedule_failed", brand_id=str(ctx.brand_id))


# A creative still "generating" this long after it was created has lost its
# worker. Sized past the worst legitimate case -- IMAGEGEN_GATE_ATTEMPTS calls
# of up to ~5 minutes each -- so a slow, healthy job is never reaped and
# refunded underneath itself.
STUCK_AFTER = timedelta(minutes=45)


def reap_stuck_creatives(now: datetime | None = None) -> int:
    """Fail creatives a crash left mid-flight, refunding exactly the billed ones.

    Charging happens before generation, so a SIGKILL during compose used to
    leave the owner with a debit, a row that says "generating" forever, and no
    way to learn either. Idempotent: the refund key is the creative id.
    """
    now = now or datetime.now(UTC)
    failed = 0
    with session_scope() as db:
        rows = db.scalars(
            select(Creative)
            .where(
                Creative.status.in_(("generating", "composing")),
                Creative.created_at < now - STUCK_AFTER,
            )
            .limit(200)
        ).all()
        for c in rows:
            c.status, c.error = "failed", "worker lost mid-generation"
            failed += 1
            if c.billed:
                brief = db.get(Brief, c.brief_id)
                credits.refund(
                    db,
                    account_id=brief.account_id,
                    amount=credits.cost_of("generate_creative"),
                    reason="stuck_creative",
                    idempotency_key=f"refund:stuck:{c.id}",
                )
    if failed:
        log.warning("creatives_reaped", failed=failed)
    return failed


# --------------------------------------------------------------------------- #
# duplicate register
# --------------------------------------------------------------------------- #
def dedupe_register() -> dict:
    """Shared state for one job: the hashes its slides have produced, and the
    semaphore that bounds how many vendor calls run at once."""
    return {
        "hashes": {},
        "lock": asyncio.Lock(),
        "sem": asyncio.Semaphore(max(1, int(settings.imagegen_concurrency))),
    }


# The lanes whose stored background is the owner's own photograph (or the
# studio built around it) rather than a picture generated for the window.
PHOTO_LANES = frozenset({"brand_asset", "product_studio"})

# The block of words the picture must stay calm under. The CTA is a solid
# pill on its own ground, the mark has its own plate and clear space, the
# rule is a hairline -- and on poster_stack the CTA sits at the foot under a
# headline at the top, so counting it made the band the whole frame.
_WORDS = ("headline", "subhead")


class PhotoPlan:
    """One owner photograph as a slide will use it: upright bytes, its size,
    the cut-out when it is a product whose cut passed, and the subject's box
    (from the same mask) whether or not the cut passed."""

    __slots__ = ("asset_id", "image", "mime", "kind", "size", "cut", "bbox", "trusted")

    def __init__(self, asset_id: str, image: bytes, mime: str, kind: str, size: tuple[int, int]):
        self.asset_id, self.image, self.mime = asset_id, image, mime
        self.kind, self.size = kind, size
        self.cut: product.Cutout | None = None
        self.bbox: tuple[int, int, int, int] | None = None
        self.trusted = False

    @property
    def cut_ok(self) -> bool:
        return self.cut is not None and self.cut.ok and self.cut.rgba is not None

    @property
    def focus(self) -> tuple[int, int, int, int] | None:
        return self.bbox if self.trusted else None


def _read_photo(snap: BrandAssetSnapshot) -> PhotoPlan:
    from app.creative import photo_quality

    raw = r2.get(snap.storage_key)
    image, mime, size = photo_quality.upright(raw, snap.mime)
    if size is None:
        size = compose.image_size(image)
    plan = PhotoPlan(snap.id, image, mime, snap.kind or "other", size)
    if snap.kind == "product" and settings.cutout_enabled:
        plan.cut = product.cutout(image)
        if plan.cut.bbox != (0, 0, 0, 0):
            plan.bbox = plan.cut.bbox
            plan.trusted = (
                product.MIN_COVERAGE <= plan.cut.coverage <= product.MAX_COVERAGE
                and plan.cut.soft_share <= product.MAX_SOFT_SHARE
            )
    else:
        plan.bbox, plan.trusted = product.subject_box(image)
    return plan


async def _plan_photos(
    units: list[Slide], assets: dict[str, BrandAssetSnapshot], resolved: dict[int, str]
) -> dict[int, PhotoPlan]:
    """Every owner photograph this job will use, read once (the mask is the
    slow part, and it used to run inside the render of every slide).

    One asset that will not come back is one slide's problem, never the job's.
    This runs before the layout gate and outside the per-slide gather, so an
    exception here leaves the agent with a tool error instead of a result --
    a transient R2 hiccup on slide 3 of a carousel would cost the owner the
    other five. A read that fails is logged and left out; _build_one tries
    that one asset again inside its own slide, under the gather that fails
    slides one at a time. Nothing is substituted: the owner asked for their
    photograph, so the slide either gets it or fails saying so.
    """
    plans: dict[int, PhotoPlan] = {}
    by_asset: dict[str, PhotoPlan] = {}
    for u in units:
        ref = resolved.get(u.position)
        if not ref or ref not in assets:
            continue
        if ref not in by_asset:
            try:
                by_asset[ref] = await asyncio.to_thread(_read_photo, assets[ref])
            except Exception as exc:  # noqa: BLE001 - one asset, one slide
                log.warning("owner_photo_unreadable", asset_id=ref, error=str(exc)[:200])
                continue
        plans[u.position] = by_asset[ref]
    return plans


def _set_template(brief: CreativeBrief, slide: Slide, template: str) -> None:
    """Change one slide's layout, in the brief AND in the slide object in hand.

    units() builds a single post's Slide detached from the brief and stamps the
    brief's template_id onto it, and template_for() reads the slide's first --
    so setting the brief's alone changed nothing the compositor would see. The
    repair ladder re-rendered the very layout it was trying to leave, scored
    that frame, returned it under the new name, and the creative row recorded a
    template the picture had never been set in. A free repair that repaired
    nothing and said it had is worse than one that refuses.
    """
    slide.template_id = template
    if not brief.is_carousel():
        brief.template_id = template
        for sl in brief.slides:
            sl.template_id = None


# Layouts that set no word on the picture, in the order a subject that the
# words would otherwise sit on is moved to: photo above a panel, photo below
# a band, photo framed on a card.
PANEL_TEMPLATES = ("split_card", "top_band", "frame_card")
# The photo window each panel layout shows at ordinary copy length, as
# fractions of the canvas (x inset, height share) -- the measured figures,
# used to pick a layout before its exact window has been measured.
_PANEL_WINDOWS = {"split_card": (0.0, 0.62), "top_band": (0.0, 0.70), "frame_card": (0.087, 0.48)}


def _subject_on_canvas(plan: PhotoPlan, w: int, h: int) -> tuple[float, float, float, float] | None:
    """Where the photo's subject lands on a w x h window after the
    subject-aware cover crop (or the letterbox), as fractions of it."""
    if plan.focus is None:
        return None
    iw, ih = plan.size
    scale = max(w / iw, h / ih)
    cw, ch = w / scale, h / scale
    at = compose._focus_crop(plan.size, (cw, ch), plan.focus)
    x0, y0, x1, y1 = plan.focus
    if at is None:
        fit = min(w / iw, h / ih)
        ox, oy = (w - iw * fit) / 2, (h - ih * fit) / 2
        return (ox + x0 * fit) / w, (oy + y0 * fit) / h, (ox + x1 * fit) / w, (oy + y1 * fit) / h
    left, top = at
    return (
        (x0 - left) * scale / w,
        (y0 - top) * scale / h,
        (x1 - left) * scale / w,
        (y1 - top) * scale / h,
    )


def _panel_for(plan: PhotoPlan, w: int, h: int) -> str:
    """The panel layout whose window shows the photo's subject whole without
    a letterbox, in PANEL_TEMPLATES order; the first when none does."""
    for name in PANEL_TEMPLATES:
        inset, share = _PANEL_WINDOWS[name]
        ww, wh = round(w * (1 - 2 * inset)), round(h * share)
        iw, ih = plan.size
        scale = max(ww / iw, wh / ih)
        if compose._focus_crop(plan.size, (ww / scale, wh / scale), plan.focus) is not None:
            return name
    return PANEL_TEMPLATES[0]


def _choose_photo_layouts(
    brief: CreativeBrief, units: list[Slide], photos: dict[int, PhotoPlan]
) -> dict[int, tuple[str, str, str]]:
    """Per slide, the layout a photo slide is set in. Returns the switches
    made: position -> (from, to, why).

    A product whose cut passed is stood in a studio, and the words go to the
    lower third (centred type and a product fight for the same pixels). A
    photo used whole -- a shop, a team, a product whose cut was refused --
    keeps a type-over-photo layout only when its subject stays clear of
    where that layout sets the words; otherwise the words move to a panel.
    """
    w, h = brief.pixel_size()
    switches: dict[int, tuple[str, str, str]] = {}
    for slide in units:
        plan = photos.get(slide.position)
        if plan is None:
            continue
        was = brief.template_for(slide)
        if plan.cut_ok:
            if was == "centered_overlay":
                _set_template(brief, slide, product.PREFERRED_TEMPLATE)
                switches[slide.position] = (was, product.PREFERRED_TEMPLATE, "product_cutout")
            continue
        zone = shotplan.TYPE_ZONES.get(was)
        subject = _subject_on_canvas(plan, w, h)
        if zone is None or subject is None:
            continue
        zl, zt, zr, zb = zone
        sl, st, sr, sb = subject
        if sl < zr and zl < sr and st < zb and zt < sb:
            to = _panel_for(plan, w, h)
            _set_template(brief, slide, to)
            switches[slide.position] = (was, to, "subject_under_type")
    if switches:
        log.info("photo_layouts_chosen", switches={str(k): v for k, v in switches.items()})
    return switches


_SMALL_PHOTO_HINT = (
    "Nothing was made and nothing was charged. The owner's photo on the slide(s) named has too "
    "few pixels for any layout: filling the picture window would enlarge it more than "
    f"{compose.MAX_PHOTO_UPSCALE}x and it would ship soft. Ask the owner to send the original "
    "photo as a DOCUMENT (WhatsApp shrinks a photo it sends as a picture to about 1280px; a "
    "document arrives at full size), then call the tool again; or call it again without "
    "reference_asset_id so the picture is generated instead."
)


def _scale_needed(plan: PhotoPlan, layout: dict | None, w: int, h: int) -> float:
    win = compose.photo_window(layout, w, h)
    return max((win[2] - win[0]) / plan.size[0], (win[3] - win[1]) / plan.size[1])


async def _photo_resolution_gate(
    brief: CreativeBrief,
    units: list[Slide],
    photos: dict[int, PhotoPlan],
    layouts: dict[int, dict],
    brand_snapshot,
    switches: dict[int, tuple[str, str, str]],
) -> dict | None:
    """Refuse -- or move to a smaller window -- any photo slide whose picture
    would have to be enlarged past MAX_PHOTO_UPSCALE. Runs after the layout
    gate, because the window is what the gate measured. A slide moved to a
    panel is re-proven, and its report replaces the old one."""
    w, h = brief.pixel_size()
    small = []
    for slide in units:
        plan = photos.get(slide.position)
        if plan is None or plan.cut_ok:
            continue
        needed = _scale_needed(plan, layouts.get(slide.position), w, h)
        if needed <= compose.MAX_PHOTO_UPSCALE:
            continue
        was = brief.template_for(slide)
        found = None
        for name in PANEL_TEMPLATES:
            if name == was:
                continue
            _set_template(brief, slide, name)
            trial = brief.units()[units.index(slide)] if not brief.is_carousel() else slide
            try:
                report = await compose.check_layout(brief, trial, brand_snapshot)
            except compose.TextDoesNotFit:
                continue
            if _scale_needed(plan, report, w, h) <= compose.MAX_PHOTO_UPSCALE:
                found, layouts[slide.position] = name, report
                break
        if found is None:
            _set_template(brief, slide, was)
            small.append(
                {
                    "slide": slide.position,
                    "asset_id": plan.asset_id,
                    "pixels": f"{plan.size[0]}x{plan.size[1]}",
                    "needs": f"{needed:.2f}x",
                }
            )
            continue
        switches[slide.position] = (was, found, "photo_too_small_for_window")
        log.info("photo_layout_for_pixels", position=slide.position, was=was, now=found)
    if not small:
        return None
    log.warning("photo_too_small_refused", slides=small)
    return {
        "ok": False,
        "reason": "photo_too_small",
        "charged": 0,
        "slides": small,
        "hint": _SMALL_PHOTO_HINT,
    }


_SMALL_PRODUCT_HINT = (
    "Nothing was made and nothing was charged. On the slide(s) named the copy leaves so "
    "little of the picture clear that the owner's product would be composited as a thumbnail "
    "on an empty backdrop, and no layout has room for it at this length. Shorten the headline "
    "or the subhead and call the tool again -- the product is what the owner judges the card "
    "by, and a card that shows it small needs a revision on the first result."
)


def _product_share(
    plan: PhotoPlan, layout: dict | None, w: int, h: int
) -> tuple[float, tuple[int, int]]:
    """How much of its window the product on this slide would cover, and that
    window's size."""
    win = compose.photo_window(layout, w, h)
    size = (win[2] - win[0], win[3] - win[1])
    free = _free_rect(layout, win, w, h)
    return product.place_share(plan.cut.rgba.size, size, free), size


async def _product_size_gate(
    brief: CreativeBrief,
    units: list[Slide],
    photos: dict[int, PhotoPlan],
    layouts: dict[int, dict],
    brand_snapshot,
    switches: dict[int, tuple[str, str, str]],
) -> dict | None:
    """Move -- or refuse -- any product slide whose cut-out the copy would
    shrink below product.MIN_PLACE_SHARE of its window.

    place() scaled the cut to whatever the free rectangle left, with no floor
    at all: a long headline and subhead on poster_stack or lower_third stood a
    bottle at 174x310 on a 1080x1350 card and FIT_JS reported nothing wrong,
    because the subject WAS inside the window, clear of the words and inside
    the safe zone. Small is not a violation of the frame, it is a violation of
    the point, so it is settled here -- before the charge, where a layout with
    a real window can still be chosen -- and not by refusing a finished render.
    Mirrors _photo_resolution_gate, which does the same for a photo with too
    few pixels.
    """
    w, h = brief.pixel_size()
    small = []
    for slide in units:
        plan = photos.get(slide.position)
        if plan is None or not plan.cut_ok:
            continue
        share, size = _product_share(plan, layouts.get(slide.position), w, h)
        if share >= product.MIN_PLACE_SHARE:
            continue
        was = brief.template_for(slide)
        found = None
        for name in PANEL_TEMPLATES:
            if name == was:
                continue
            _set_template(brief, slide, name)
            trial = brief.units()[units.index(slide)] if not brief.is_carousel() else slide
            try:
                report = await compose.check_layout(brief, trial, brand_snapshot)
            except compose.TextDoesNotFit:
                continue
            if _product_share(plan, report, w, h)[0] >= product.MIN_PLACE_SHARE:
                found, layouts[slide.position] = name, report
                break
        if found is None:
            _set_template(brief, slide, was)
            small.append(
                {
                    "slide": slide.position,
                    "asset_id": plan.asset_id,
                    "window": f"{size[0]}x{size[1]}",
                    "covers": f"{share:.0%}",
                }
            )
            continue
        switches[slide.position] = (was, found, "product_too_small_for_window")
        log.info("product_layout_for_size", position=slide.position, was=was, now=found)
    if not small:
        return None
    log.warning("product_too_small_refused", slides=small)
    return {
        "ok": False,
        "reason": "product_too_small",
        "charged": 0,
        "slides": small,
        "hint": _SMALL_PRODUCT_HINT,
    }


def cutout_key(brand_id: str, creative_id: str) -> str:
    """Where a product-studio creative keeps its cut-out (see cutout_png)."""
    return r2.key_for(brand_id, creative_id, "cutout.png")


# Everything the product must keep clear of, besides the words: the mark and
# its clear space, the brand name, the pill. The rule is a hairline under
# the words and inside their block.
_KEEP_OUT = ("headline", "subhead", "cta", "logo", "brandline")
# Clear space kept between the product and anything it must not touch, as a
# share of the canvas width: a product against the CTA reads as touching it.
_KEEP_OUT_MARGIN = 0.03


def _free_rect(
    layout: dict | None, window: tuple[int, int, int, int], w: int, h: int
) -> tuple[int, int, int, int]:
    """The largest rectangle inside the photo window and the safe zone that
    the measured layout leaves clear of every word and mark (each grown by a
    margin), in WINDOW coordinates. Where the product goes."""
    pad = compose.padding_for(w, h)
    left = max(window[0], pad["pad_x"])
    top = max(window[1], pad["pad_top"])
    right = min(window[2], w - pad["pad_x"])
    bottom = min(window[3], h - pad["pad_bottom"])
    rect = [float(left), float(top), float(right), float(bottom)]
    boxes = (layout or {}).get("boxes") or {}
    grow = w * _KEEP_OUT_MARGIN
    keep = []
    for cls in _KEEP_OUT:
        b = boxes.get(cls)
        if b:
            g = compose.LOGO_CLEAR * (b["b"] - b["t"]) if cls == "logo" else 0.0
            keep.append(
                (b["l"] - grow - g, b["t"] - grow - g, b["r"] + grow + g, b["b"] + grow + g)
            )
    # Greedy: for each keep-out that still intersects, keep the largest of
    # the four rectangles left around it. Words sit in one block, so this
    # finds the band above (or below, or beside) them.
    for _ in range(len(keep) + 1):
        hit = next(
            (
                k
                for k in keep
                if rect[0] < k[2] and k[0] < rect[2] and rect[1] < k[3] and k[1] < rect[3]
            ),
            None,
        )
        if hit is None:
            break
        options = [
            [rect[0], rect[1], rect[2], hit[1]],  # above
            [rect[0], hit[3], rect[2], rect[3]],  # below
            [rect[0], rect[1], hit[0], rect[3]],  # left of
            [hit[2], rect[1], rect[2], rect[3]],  # right of
        ]
        rect = max(options, key=lambda r: max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1]))
    return (
        int(round(rect[0] - window[0])),
        int(round(rect[1] - window[1])),
        int(round(rect[2] - window[0])),
        int(round(rect[3] - window[1])),
    )


def _text_box_in_window(
    layout: dict | None, window: tuple[int, int, int, int]
) -> tuple[float, float, float, float] | None:
    """The measured type block as fractions of the photo window (l, t, r, b),
    or None when no word sits on the picture (a panel layout, or no report)."""
    boxes = (layout or {}).get("boxes") or {}
    found = [boxes[c] for c in _WORDS if boxes.get(c)]
    if not found:
        return None
    wl, wt, wr, wb = window
    left = max(wl, min(b["l"] for b in found))
    top = max(wt, min(b["t"] for b in found))
    right = min(wr, max(b["r"] for b in found))
    bottom = min(wb, max(b["b"] for b in found))
    if right <= left or bottom <= top:
        return None
    ww, wh = wr - wl, wb - wt
    return ((left - wl) / ww, (top - wt) / wh, (right - wl) / ww, (bottom - wt) / wh)


def _size_fault(provider, got: tuple[int, int], asked: tuple[int, int], floor) -> str:
    """Why `got` is not the frame that was asked for, or '' when it is.

    A vendor that picks its own size is held to the one thing that matters
    downstream: covering the WINDOW the compositor will fit it to, within the
    pixels the compositor allows. This used to state its own percentage of the
    asked ratio (0.5%), which is a different quantity from the one
    fit_background measures and disagreed with it in both directions. It let
    through -- and paid for -- frames the compositor then refused: 0.5% of a
    1080x1920 story's ratio is up to 9.6px of window crop against the 4px of
    compose.GENERATED_CROP_TOLERANCE, and PictureMismatch is deliberately not
    retried, so the slide failed after the money was spent. It also added to
    the asked frame's own allowance instead of counting the trim once. Both
    errors disappear when the gate measures what the compositor measures.
    """
    if getattr(provider, "exact_size", False):
        return "" if got == asked else f"asked {asked[0]}x{asked[1]}, got {got[0]}x{got[1]}"
    need = floor or asked
    crop = crop_for(got, need)
    if crop > compose.GENERATED_CROP_TOLERANCE:
        return (
            f"ratio {got[0]}x{got[1]} does not cover the {need[0]}x{need[1]} window "
            f"(crop {crop:.1f}px)"
        )
    if got[0] < need[0] or got[1] < need[1]:
        return f"{got[0]}x{got[1]} is below the {need[0]}x{need[1]} it must cover"
    return ""


def _ratio_crop(ratio: float, window: tuple[int, int]) -> float:
    """Pixels of `window` lost by any frame of `ratio` that covers it, at any
    size. The shape alone decides this, so it can be measured before a vendor
    is asked for anything."""
    bw, bh = window
    return bh * ratio - bw if ratio > bw / bh else bw / ratio - bh


def _shape_the_vendor_cannot_make(provider, asked: tuple[int, int], window) -> str:
    """Why this vendor can never render a picture for `window`, or ''.

    A vendor that renders one of a fixed list of aspect ratios (replicate
    picks the nearest of eleven) cannot be rerolled into a shape that is not
    on the list: every attempt is the same call at the same settings, only the
    seed moves, and a seed does not move the frame. Since the picture is now
    generated for the WINDOW the layout shows, the windowed templates ask for
    shapes no preset comes near -- split_card's 1080x842 is 1.282 and the
    nearest preset is 5:4, which cuts 22px off the window. Left to the gate
    that is IMAGEGEN_GATE_ATTEMPTS calls, all six charged at the vendor, all
    six refused as wrong_size, on every slide of every job. It is the vendor's
    limitation and it is knowable at the door, so it is named at the door and
    nothing is asked of them.
    """
    delivered = getattr(provider, "delivered_ratio", None)
    if delivered is None:
        return ""
    ratio = delivered(*asked)
    lost = _ratio_crop(ratio, window)
    if lost <= compose.GENERATED_CROP_TOLERANCE:
        return ""
    return (
        f"{provider.name} renders {ratio:.4f}:1 for a {asked[0]}x{asked[1]} ask, and no "
        f"picture of that shape covers this layout's {window[0]}x{window[1]} window "
        f"({lost:.1f}px of it would be cut)"
    )


class BackgroundRejected(RuntimeError):
    """No acceptable picture in the allowed attempts. The slide fails; nothing
    that was rejected is ever delivered."""

    def __init__(self, message: str, *, cost_micros: int = 0, rejections: list | None = None):
        super().__init__(message)
        self.cost_micros = cost_micros
        self.rejections = rejections or []


async def _generate_checked(
    ctx: ToolContext,
    provider,
    brief: CreativeBrief,
    slide: Slide,
    creative_id: uuid.UUID,
    prompt: str,
    negative: str,
    size: tuple[int, int],
    register: dict | None,
    stage: str,
    floor: tuple[int, int] | None = None,
    budget_micros: int | None = None,
):
    """Generate, inspect, and regenerate until a picture passes -- or fail.

    `budget_micros` is what is left of this slide's vendor cap; None means
    the whole of settings.imagegen_gate_budget_micros.

    `floor` is the window the picture must cover (never enlarged to). A
    vendor that picks its own size from a ratio (replicate, bfl) is held to
    covering that window within the compositor's own crop tolerance; one that
    returns exactly what it is asked is held to exactly that.

    Every attempt is the SAME call at the SAME settings; only the prompt gains
    a sentence about what was wrong, and the seed moves. Reasons to reject:
    anything bggate.inspect reports, a size other than the one asked for, or a
    picture an earlier slide of this carousel already produced. Every rejection
    is logged with its reason and attempt number. On exhaustion this raises:
    the slide is failed and refunded, and the owner is told.
    """
    attempts = max(1, int(settings.imagegen_gate_attempts))
    # `budget_micros` is what is LEFT of this slide's cap. The final check can
    # buy one more picture for a slide that already bought some, and without
    # this it would open a second full cap -- one credit in, twice the vendor
    # spend out, which is the hole the cap was put there to close.
    budget = int(settings.imagegen_gate_budget_micros if budget_micros is None else budget_micros)
    gw, gh = size
    cannot = _shape_the_vendor_cannot_make(provider, (gw, gh), floor or (gw, gh))
    if cannot:
        log.error("imagegen_shape_unreachable", provider=provider.name, detail=cannot)
        raise BackgroundRejected(cannot, cost_micros=0)
    sem = (register or {}).get("sem") or asyncio.Semaphore(1)
    # The mock draws a gradient for tests and local development; there is no
    # model output to inspect. Every real vendor is inspected, no exceptions.
    inspected = provider.name != "mock"
    cost, rejections, reasons_so_far = 0, [], []
    per_call = int(getattr(provider, "cost_micros_per_image", 0) or 0)
    for attempt in range(1, attempts + 1):
        # The last attempt is the last the cap OR the count allows.
        last = attempt == attempts or bool(budget and cost + 2 * per_call > budget)
        seed = slide.visual_direction.seed
        if attempt > 1:
            seed = shotplan.seed_for(shotplan.brief_key(brief), slide.position, salt=attempt - 1)
        name = f"{stage}:imagegen" if attempt == 1 else f"{stage}:imagegen_retry{attempt - 1}"
        async with sem:
            with ctx.trace.stage(name, provider=provider.name, attempt=attempt):
                res = await provider.generate(
                    ImageRequest(
                        prompt=bggate.corrected(prompt, reasons_so_far),
                        negative=negative,
                        width=gw,
                        height=gh,
                        seed=seed,
                        style=slide.visual_direction.mood,
                    )
                )
        cost += int(res.cost_micros or 0)
        per_call = int(res.cost_micros or 0) or per_call

        reasons: list[str] = []
        notes = ""
        got = await asyncio.to_thread(compose.image_size, res.data)
        fault = _size_fault(provider, got, (gw, gh), floor)
        if fault:
            reasons.append("wrong_size")
            notes = fault
        if not reasons and register is not None:
            if await _register_or_reroll(register, slide, res.data, force=last):
                reasons.append("duplicate_of_earlier_slide")
        if not reasons and inspected:
            with ctx.trace.stage(f"{stage}:inspect{attempt}"):
                try:
                    verdict = await bggate.inspect(res.data)
                except bggate.InspectionUnavailable as exc:
                    # The pictures this slide has already bought were bought
                    # whether or not the inspector ever answered about them.
                    exc.cost_micros += cost
                    raise
            # The look is paid for whatever it decides, and a slide rejected
            # twice pays for three of them. This used to be read and dropped,
            # so the row told the owner the slide cost what the pictures cost
            # while the gate around them had spent more on top -- and once the
            # final check started pricing ITS looks, the ledger disagreed with
            # itself about which inspections count.
            cost += int(verdict.cost_micros or 0)
            reasons, notes = list(verdict.reasons), verdict.notes
        if not reasons:
            if rejections:
                log.info(
                    "background_accepted_after_retry",
                    creative_id=str(creative_id),
                    position=slide.position,
                    attempt=attempt,
                )
            return res, cost, rejections

        rejections.append({"attempt": attempt, "reasons": reasons, "notes": notes})
        reasons_so_far.extend(reasons)
        log.warning(
            "background_rejected",
            creative_id=str(creative_id),
            position=slide.position,
            attempt=attempt,
            of=attempts,
            reasons=reasons,
            notes=notes,
            job_id=res.job_id,
            cost_micros_so_far=cost,
        )
        if budget and cost + per_call > budget:
            log.warning(
                "background_gate_budget_reached",
                creative_id=str(creative_id),
                position=slide.position,
                attempts=attempt,
                cost_micros=cost,
                budget_micros=budget,
            )
            break
    seen = sorted({r for rej in rejections for r in rej["reasons"]})
    raise BackgroundRejected(
        f"no acceptable picture in {len(rejections)} attempts ({', '.join(seen)})",
        cost_micros=cost,
        rejections=rejections,
    )


class CompositeRejected(RuntimeError):
    """The finished card did not pass the final check, and no free rearrangement
    of it passed either.

    Nothing is delivered and nothing is stored: the slide fails and is
    refunded, exactly as a rejected background is. Refusing to render beats
    shipping a flawed frame -- the owner pays a lot and the first result is
    supposed to need no revision, so a card nobody would have approved is not
    an acceptable thing to send while we wait to be told.

    Carries `cost_micros` like BackgroundRejected so vendor spend that really
    happened is not lost from the ledger, and `hint` so the agent can say
    something useful instead of "it failed".
    """

    def __init__(
        self,
        message: str,
        *,
        cost_micros: int = 0,
        faults: list[str] | None = None,
        reasons: list[str] | None = None,
        hint: str = "",
    ):
        super().__init__(message)
        self.cost_micros = cost_micros
        self.faults = list(faults or [])
        self.reasons = list(reasons or [])
        self.hint = hint


_OWNER_PHOTO_REFUSAL = (
    "The finished card did not pass the final check, and no other layout fixed it. This "
    "picture is the owner's own, so it is never replaced with a bought one without being "
    "asked. Tell the owner in one plain sentence what is wrong with it, and offer the two "
    "things that would fix it: another photo (ask for the original sent as a DOCUMENT, "
    "because WhatsApp shrinks a photo sent as a picture), or a different layout."
)
_GENERATED_REFUSAL = (
    "The finished card did not pass the final check. Another layout was tried and a "
    "corrected picture was bought, and neither passed, so nothing was delivered and the "
    "credit is refunded. Tell the owner plainly and suggest a shorter headline or a "
    "different subject for the picture."
)


def _copy_for_gate(brief: CreativeBrief, slide: Slide, brand_snapshot) -> dict[str, str]:
    """The words the card is supposed to show, for the inspector's prompt.

    Without them the inspector cannot do the one thing here that no
    measurement can: tell the headline it is meant to see from lettering the
    image model invented into a shop sign.
    """
    return {
        "headline": str(slide.headline or brief.headline or ""),
        "subhead": str(slide.subhead or brief.subhead or ""),
        "cta": str(brief.cta or ""),
        "brand": str(getattr(brand_snapshot, "name", "") or ""),
    }


async def _final_check(
    ctx: ToolContext,
    brief: CreativeBrief,
    slide: Slide,
    brand_snapshot,
    creative_id: uuid.UUID,
    *,
    image: bytes,
    mime: str,
    png: bytes,
    final: bytes,
    report: dict,
    generated: bool,
    focus: tuple[int, int, int, int] | None,
    subject: tuple[int, int, int, int] | None,
    stage: str,
    lane: str,
    buy: Any = None,
) -> dict:
    """Look at the finished card, repair it for free, and only then spend.

    This runs on EVERY lane -- generated, owner photo, product studio, reused
    and recomposed -- after the export and before anything is uploaded or
    marked ready. Until it passes, nothing has been delivered and nothing is
    stored, which is the point: before this, no check in the product had ever
    looked at the frame the client receives.

    The order is deliberate and it is about money. First the deterministic
    measurements, which are free. Then up to `composite_free_variants`
    re-composes of the SAME picture into other layouts, which cost 3.2-5.6s of
    Chromium each (measured) and nothing at the vendor. Only when no
    arrangement of the picture they already have is good enough may `buy` be
    called, and only the generated lane passes one: an owner's photograph, a
    studio built from it and a reused background are never replaced with a
    bought picture, because the owner did not ask us to replace their picture.
    A free lane with nothing good enough refuses, with something the agent can
    act on.

    Returns what should be delivered, or raises CompositeRejected.
    """
    w, h = brief.pixel_size()
    started = brief.template_for(slide)
    copy = _copy_for_gate(brief, slide, brand_snapshot)
    # The mock draws a gradient for tests and local development, so there is no
    # model output to judge. That is the background gate's exemption, and this
    # is keyed on the same thing IT is keyed on: the image provider this run is
    # configured with. It used to be keyed on the LANE, and "recomposed",
    # "brand_asset", "product_studio" and "reused" are not provider names --
    # so in a wholly mocked run (CI, or a developer with IMAGEGEN_PROVIDER=mock
    # and no ANTHROPIC_API_KEY) every revision and every free-lane slide was
    # sent to an inspector that is not there, died on InspectionUnavailable,
    # and no revision could be made at all.
    looked_at = settings.imagegen_provider != "mock"
    spent = 0
    source: dict[str, Any] = {"image": image, "mime": mime, "size": compose.image_size(image)}
    # The subject box is used where the pipeline ALREADY has one, and it is
    # never gone looking for here. An owner's photograph arrives with its
    # subject located (_plan_photos, once, before the charge) and a product
    # studio reports where it stood its cut-out; both are free and both are
    # exactly the cases where a window can behead something. Running the
    # salient-object model again on a generated picture would add ~2s of
    # serialised CPU per slide -- 12s on a carousel -- to answer a question
    # that picture cannot get wrong: it is made for its window, so no window
    # crops it, and the inspector reads text_covers_subject on the frame
    # itself. Measure what is known; do not buy an opinion twice.
    found: dict[str, Any] = {"subject": subject if subject is not None else focus}

    def look(template: str, a_png: bytes, a_final: bytes, a_report: dict):
        return compositeqa.Variant(
            template,
            a_png,
            a_final,
            a_report,
            compositeqa.assess(
                a_report,
                template=template,
                jpeg=a_final,
                source_size=source["size"],
                subject=found["subject"],
                focus=focus,
            ),
        )

    async def render(template: str):
        """The same picture, set in another layout. Free: no vendor call."""
        was = brief.template_for(slide)
        _set_template(brief, slide, template)
        try:
            with ctx.trace.stage(f"{stage}:variant_{template}"):
                fresh = await compose.check_layout(brief, slide, brand_snapshot)
                v_png, v_report = await compose.compose_with_report(
                    brief,
                    slide,
                    brand_snapshot,
                    source["image"],
                    source["mime"],
                    layout=fresh,
                    generated=generated,
                    focus=focus,
                    subject=subject,
                    keep_sources=brief.is_reel(),
                )
                v_final = await asyncio.to_thread(compose.export_jpeg, v_png, (w, h))
        except (
            compose.TextDoesNotFit,
            compose.PictureMismatch,
            compose.PhotoTooSmall,
            compose.BrandFontUnavailable,
        ) as exc:
            # This layout cannot take this slide -- the copy does not fit it,
            # or a picture made for one window does not fill another's. Not a
            # failure: just one fewer free option.
            log.info(
                "composite_variant_unavailable",
                position=slide.position,
                template=template,
                why=repr(exc)[:140],
            )
            return None
        finally:
            _set_template(brief, slide, was)
        return look(template, v_png, v_final, v_report)

    async def inspected(variant):
        nonlocal spent
        if not looked_at:
            return None
        with ctx.trace.stage(f"{stage}:final_gate"):
            try:
                verdict = await finalgate.inspect(variant.jpeg, **copy)
            except finalgate.InspectionUnavailable as exc:
                # An outage is a refusal, and a refusal still shows what it
                # cost: the attempts the model answered before it gave up, plus
                # every look this slide had already paid for. _build_one adds
                # the picture on top.
                exc.cost_micros += spent
                raise
        spent += int(verdict.cost_micros or 0)
        return verdict

    free = max(0, int(settings.composite_free_variants))
    paid = max(0, int(settings.composite_paid_retries)) if buy is not None else 0

    for purchase in range(paid + 1):
        first = look(brief.template_for(slide), png, final, report)
        best = await compositeqa.best_free_variant(first, render, limit=free)
        verdict = await inspected(best)

        curable = (
            verdict is not None
            and not verdict.ok
            and (set(verdict.reasons) - finalgate.PICTURE_REASONS)
        )
        if curable:
            # The inspector saw something the measurements could not. Try the
            # layouts its reasons point at, and look once more -- once, so a
            # disagreeing inspector cannot spend the afternoon. Reasons that
            # are the PICTURE's fault skip this: no arrangement of a
            # photograph with a melted hand in it is the right arrangement,
            # and rendering three of them to find that out wastes three
            # seconds and risks shipping the fourth.
            moved = await compositeqa.best_free_variant(
                best, render, limit=free, codes=finalgate.as_codes(verdict.reasons),
                must_change=True,
            )  # fmt: skip
            if moved is not best:
                second = await inspected(moved)
                if second is not None and second.ok and moved.assessment.ok:
                    best, verdict = moved, second
                elif second is not None and len(second.reasons) < len(verdict.reasons):
                    best, verdict = moved, second

        if best.assessment.ok and (verdict is None or verdict.ok):
            _set_template(brief, slide, best.template)
            return {
                "png": best.png,
                "final": best.jpeg,
                "report": best.report,
                "template": best.template,
                "image": source["image"],
                "mime": source["mime"],
                "cost_micros": spent,
                "score": verdict.score if verdict is not None else best.score,
                "qa": best.assessment.as_dict(),
                "reasons": [],
                # What the inspector SAW, even when it passed the card: a note
                # about a frame nobody objected to is exactly what a later
                # calibration against the owner's answer needs.
                "notes": verdict.notes if verdict is not None else best.assessment.notes,
                "bought": purchase,
                "was_template": started,
            }

        faults = list(best.assessment.faults)
        reasons = list(verdict.reasons) if verdict is not None else []
        if purchase == paid:
            break
        # Nothing free was good enough. The generated lane may buy ONE more
        # picture, corrected by what was wrong with this one.
        again, again_mime, extra = await buy(faults, reasons, brief.template_for(slide))
        # Paid for whether or not it produced anything usable: the spend is
        # real, so it goes on the tally that rides home on the refusal too.
        spent += int(extra or 0)
        if again is None:
            break
        source["image"], source["mime"] = again, again_mime
        source["size"] = compose.image_size(source["image"])
        _set_template(brief, slide, started)
        with ctx.trace.stage(f"{stage}:recompose"):
            layout_again = await compose.check_layout(brief, slide, brand_snapshot)
            png, report = await compose.compose_with_report(
                brief, slide, brand_snapshot, source["image"], source["mime"],
                layout=layout_again, generated=generated, focus=focus, subject=subject,
                keep_sources=brief.is_reel(),
            )  # fmt: skip
            final = await asyncio.to_thread(compose.export_jpeg, png, (w, h))

    _set_template(brief, slide, started)
    log.error(
        "composite_refused",
        creative_id=str(creative_id),
        position=slide.position,
        lane=lane,
        faults=faults,
        reasons=reasons,
        cost_micros=spent,
    )
    said = ", ".join(faults + reasons) or "the final check"
    raise CompositeRejected(
        f"the finished slide did not pass the final check ({said})",
        cost_micros=spent,
        faults=faults,
        reasons=reasons,
        hint=_GENERATED_REFUSAL if buy is not None else _OWNER_PHOTO_REFUSAL,
    )


async def _register_or_reroll(
    register: dict, slide: Slide, image: bytes, *, force: bool = False
) -> bool:
    """Record this slide's picture. True when it repeats an earlier slide.

    Held under a lock because slides finish in whatever order the vendor
    returns them, and two slides checking an empty register at the same moment
    would both believe they were first. The EARLIER slide position always
    keeps its picture; the later one is the one asked to try again.

    `force` records unconditionally -- used after a re-roll, which is not
    allowed to trigger a second one.
    """
    try:
        h = await asyncio.to_thread(dedupe.dhash, image)
    except Exception:  # noqa: BLE001 - a hash failure must never fail a slide
        log.warning("dedupe_hash_failed", position=slide.position)
        return False
    async with register["lock"]:
        if not force:
            for pos, seen in register["hashes"].items():
                if pos < slide.position and dedupe.distance(h, seen) <= dedupe.DUPLICATE_DISTANCE:
                    return True
        register["hashes"][slide.position] = h
    return False


# --------------------------------------------------------------------------- #
async def _build_one(
    ctx: ToolContext,
    brief: CreativeBrief,
    slide: Slide,
    creative_id: uuid.UUID,
    brand_snapshot,
    assets: dict[str, BrandAssetSnapshot],
    resolved: dict[int, str] | None = None,
    reuse: dict[int, tuple[str, str]] | None = None,
    register: dict | None = None,
    delivery: _Delivery | None = None,
    layout: dict | None = None,
    plan: PhotoPlan | None = None,
) -> str:
    w, h = brief.pixel_size()
    ref = (resolved or {}).get(slide.position)
    focus: tuple[int, int, int, int] | None = None
    subject: tuple[int, int, int, int] | None = None
    cutout_png: bytes | None = None
    kept_key: str | None = None  # set only when this slide's picture was KEPT
    stage = f"slide{slide.position}"
    job_id, cost_micros = None, 0  # set only when a vendor was paid
    gate: dict | None = None  # set only when a picture was generated and inspected
    # The window the layout shows the picture through, measured before the
    # charge. The picture is made for THAT -- never for the canvas and then
    # cover-cropped by a panel, a band or a frame.
    window = compose.photo_window(layout, w, h)
    window_size = (window[2] - window[0], window[3] - window[1])
    # Only a picture made for the window is held to it exactly; the owner's
    # own photograph is whatever shape they took it in.
    generated = True

    if reuse and slide.position in reuse:
        # Picture kept from the previous version of this creative.
        kept = reuse[slide.position]
        with ctx.trace.stage(f"{stage}:reuse"):
            kept_key = kept[0]
            image = r2.get(kept_key)
            mime = "image/jpeg" if kept_key.endswith(".jpg") else "image/png"
        # The row records the lane the picture was MADE in, never the fact
        # that this version did not remake it. Writing "reused" here erased
        # brand_asset/product_studio, and the owner's next free copy change
        # was then held to the generated picture's contract and refused the
        # whole version -- recompose, _owner_photo_slides and _revision_guard
        # all read this column to tell a photograph from a made picture.
        provider_name = (kept[2] if len(kept) > 2 else "") or "reused"
        generated = provider_name not in PHOTO_LANES
        if not generated:
            # The owner's photograph, kept: cropped around its subject again.
            focus = (await asyncio.to_thread(product.subject_box, image))[0]
    elif ref and ref in assets:
        # The owner's own photograph, read once by _plan_photos: upright, with
        # its subject located. No model call, no charge.
        if plan is None:
            with ctx.trace.stage(f"{stage}:asset_fetch"):
                plan = await asyncio.to_thread(_read_photo, assets[ref])
        image, mime, focus = plan.image, plan.mime, plan.focus
        provider_name = "brand_asset"
        generated = False
        if plan.cut_ok:
            # Lay out first, place second: the studio is built at the photo
            # WINDOW's size and the product stands in the rectangle the
            # measured layout leaves clear of words and mark, so nothing is
            # cover-cropped and nothing sits on it -- by construction, and
            # then asserted by FIT_JS on the subject box. Refused cuts use
            # the photo as it was, cropped around the product.
            free = _free_rect(layout, window, w, h)
            with ctx.trace.stage(f"{stage}:product_studio"):
                studio = await asyncio.to_thread(
                    product.product_background,
                    image,
                    window_size[0],
                    window_size[1],
                    palette=dict(getattr(brand_snapshot, "palette", {}) or {}),
                    template=brief.template_for(slide),
                    cut=plan.cut,
                    free=free,
                )
            if studio is not None:
                image, mime, focus = studio[0], "image/png", None
                generated = True  # made for the window exactly, like a generated picture
                provider_name = "product_studio"
                sx0, sy0, sx1, sy1 = studio[1]["subject"]
                subject = (sx0 + window[0], sy0 + window[1], sx1 + window[0], sy1 + window[1])
                cutout_png = product.cutout_png(plan.cut, plan.asset_id)
                log.info("product_studio", position=slide.position, **studio[1])
    else:
        provider = get_provider()
        provider_name = provider.name
        # A photograph is asked for in the words a photograph is described in;
        # the rendered look is named in the negative. Applied in code so every
        # slide gets it, not only the ones the model remembered to dress up.
        prompt, negative = photoreal.photographic(
            slide.visual_direction.prompt,
            slide.visual_direction.negative_prompt,
            mood=slide.visual_direction.mood,
            category=getattr(brand_snapshot, "category", None),
            # The slide's own rung of the shot ladder. Without these two the
            # whole carousel shared one lens, one distance and one angle.
            position=slide.position,
            slide_count=len(brief.slides) if brief.is_carousel() else 1,
            # The brand's exact hex values, so the picture harmonises with
            # the type and the mark that will be laid over it.
            palette=dict(getattr(brand_snapshot, "palette", {}) or {}),
            # One photographer per brand, the same on every post (shotplan).
            style=shotplan.style_of(
                getattr(brand_snapshot, "template_prefs", None),
                str(getattr(brand_snapshot, "name", "") or ""),
            ),
            # Where the words will sit on THIS layout, measured by the gate,
            # so the model keeps that part of the frame calm -- not the
            # rung's guess by slide position, which contradicted the layout.
            template=brief.template_for(slide),
            text_box=_text_box_in_window(layout, window),
        )
        # Generated natively at the WINDOW's ratio ABOVE its size and resampled
        # down by the compositor. Never generated at a preset and cropped, and
        # never generated for the canvas and cropped by the layout.
        res, cost_micros, rejections = await _generate_checked(
            ctx,
            provider,
            brief,
            slide,
            creative_id,
            prompt,
            negative,
            generation_size_for_window(window_size, (w, h)),
            register,
            stage,
            floor=window_size,
        )
        image, mime, job_id = res.data, res.mime, res.job_id
        gate = {"attempts": len(rejections) + 1, "rejections": rejections, "raw": res.raw}

        async def buy(faults, reasons, template, _prompt=prompt, _negative=negative):
            """One more picture for this slide, corrected by what was wrong.

            Only the generated lane has this. It stays inside the SAME
            per-slide cap the background gate spends from, so a slide cannot
            quietly cost two caps because the final check asked for a second
            picture. When there is not enough left in the cap for one honest
            call, the answer is no.

            Returns (picture, mime, what it cost) -- and the cost even when
            there is no picture, because a rejected retry was still paid for
            and the ledger has to show it whether or not the slide ships.
            """
            left = int(settings.imagegen_gate_budget_micros or 0)
            if left:
                left -= int(cost_micros or 0)
                if left < int(getattr(provider, "cost_micros_per_image", 0) or 0):
                    log.warning(
                        "composite_retry_budget_reached",
                        creative_id=str(creative_id),
                        position=slide.position,
                        spent_micros=int(cost_micros or 0),
                    )
                    return None, "", 0
            try:
                again, extra, more = await _generate_checked(
                    ctx,
                    provider,
                    brief,
                    slide,
                    creative_id,
                    finalgate.corrected(
                        _prompt,
                        reasons + faults,
                        template=template,
                        text_box=_text_box_in_window(layout, window),
                    ),
                    _negative,
                    generation_size_for_window(window_size, (w, h)),
                    register,
                    f"{stage}:retry",
                    floor=window_size,
                    budget_micros=left,
                )
            except BackgroundRejected as exc:
                return None, "", int(exc.cost_micros or 0)
            gate["attempts"] += len(more) + 1
            gate["rejections"].extend(more)
            return again.data, again.mime, int(extra or 0)

    with ctx.trace.stage(f"{stage}:compose"):
        png, report = await compose.compose_with_report(
            brief,
            slide,
            brand_snapshot,
            image,
            mime,
            layout=layout,
            generated=generated,
            focus=focus,
            subject=subject,
            keep_sources=brief.is_reel(),
            kept=generated and kept_key is not None,
        )
        # PNG all the way to here; this is the single lossy encode. It also
        # refuses any frame that is not exactly the post size.
        final = await asyncio.to_thread(compose.export_jpeg, png, (w, h))

    # Nothing is uploaded, stored or delivered until the finished frame has
    # been looked at -- measured, repaired for free where it can be, and shown
    # to the inspector. Every lane, including the ones that never cost money.
    template = brief.template_for(slide)
    checked: dict | None = None
    if settings.composite_gate_enabled:
        try:
            checked = await _final_check(
                ctx,
                brief,
                slide,
                brand_snapshot,
                creative_id,
                image=image,
                mime=mime,
                png=png,
                final=final,
                report=report,
                generated=generated,
                focus=focus,
                subject=subject,
                stage=stage,
                lane=provider_name,
                buy=(
                    buy
                    if provider_name not in PHOTO_LANES | {"reused"} and gate is not None
                    else None
                ),
            )
        except (CompositeRejected, bggate.InspectionUnavailable) as exc:
            # The picture this slide had already bought was bought whatever the
            # check then decided -- and an inspector that could not be reached
            # decides nothing at all. Both refusals only know what THEY spent,
            # so the rest is added here or the failed row understates the loss.
            exc.cost_micros += int(cost_micros or 0)
            raise
        png, final, report = checked["png"], checked["final"], checked["report"]
        image, mime, template = checked["image"], checked["mime"], checked["template"]
        # Everything the final check spent: the looks, and any picture it
        # bought. When it refuses instead, the same figure rides out on
        # CompositeRejected.cost_micros and lands on the failed row.
        cost_micros += int(checked["cost_micros"] or 0)

    with ctx.trace.stage(f"{stage}:upload"):
        if kept_key is not None:
            # The background already lives in R2 under its old key; a copy per
            # revision is storage for nothing.
            bg_key = kept_key
        else:
            ext = "jpg" if "jpeg" in mime else "png"
            bg_key = r2.key_for(str(ctx.brand_id), str(creative_id), f"bg.{ext}")
            r2.put(bg_key, image, mime)
            if cutout_png is not None:
                # The cut product itself, beside the studio built around it:
                # a revision to another layout stands it in the new window
                # for nothing, instead of cropping the old composite.
                r2.put(cutout_key(str(ctx.brand_id), str(creative_id)), cutout_png, "image/png")
        composed_key = r2.key_for(str(ctx.brand_id), str(creative_id), "composed.jpg")
        composed_url = r2.put(composed_key, final, "image/jpeg")

    video_key = video_url = None
    if brief.is_reel():
        video_key, video_url = await _render_reel(ctx, creative_id, report["reel"], stage)

    with session_scope() as db:
        c = db.get(Creative, creative_id)
        c.background_key, c.background_url = bg_key, r2.public_url(bg_key)
        c.composed_key, c.composed_url = composed_key, composed_url
        c.video_key, c.video_url = video_key, video_url
        c.imagegen_provider = provider_name
        c.imagegen_job_id = (job_id or None) and str(job_id)[:120]
        c.cost_micros = int(cost_micros or 0)
        c.status = "ready"
        c.timings = dict(ctx.trace.timings)
        if checked is not None:
            # quality_score has been on this table since migration 0004 and no
            # code has ever written it. The verdict goes beside the owner's own
            # answer ('approve', 'change_picture', ...) so the thresholds can
            # one day be calibrated against approvals instead of a guess.
            c.quality_score = checked["score"]
            c.template = template
            events.record(
                db,
                kind="quality",
                account_id=ctx.account_id,
                brand_id=ctx.brand_id,
                brief_id=c.brief_id,
                creative_id=creative_id,
                meta={
                    # The QA dict carries a "score" of its own and used to be
                    # splatted LAST, so the deterministic number quietly
                    # overwrote the verdict this row exists to record: the
                    # column said 78 and the event beside it said 96, and the
                    # correlation against the owner's answer -- the one way
                    # this product gets smarter -- was being built on the wrong
                    # figure. Both are kept, under names that cannot collide.
                    **checked["qa"],
                    "lane": provider_name,
                    "template": template,
                    "was_template": checked["was_template"],
                    "score": checked["score"],
                    "measured_score": checked["qa"]["score"],
                    "reasons": checked["reasons"],
                    "inspector_notes": checked["notes"],
                    "bought": checked["bought"],
                },
            )
    if gate is not None:
        # The generation record: exactly what was asked for and what it took.
        log.info(
            "generation_record",
            creative_id=str(creative_id),
            position=slide.position,
            provider=provider_name,
            cost_micros=int(cost_micros or 0),
            attempts=gate["attempts"],
            rejections=gate["rejections"],
            **{k: v for k, v in (gate["raw"] or {}).items() if k in ("model", "size", "quality")},
        )
    url = video_url or composed_url
    if delivery is not None:
        # Out the door now. The other slides are still being made.
        with ctx.trace.stage(f"{stage}:deliver"):
            await delivery.send(slide.position, url)
    return url


async def _render_reel(ctx, creative_id: uuid.UUID, sources: dict, stage: str):
    """The card set in motion: a seven-second MP4 beside the still, in R2.

    `sources` is what compose measured and rasterised for it (the 2x frame
    without its words, the 2x finished frame, the photo window), so the reel
    moves the picture inside the window the layout shows and ends on the
    approved still. CPU-bound (PIL frames piped to ffmpeg), so it runs in a
    thread and never blocks the other slides or the WhatsApp loop.
    """
    from app.creative import reel

    w, h = 1080, 1920
    with ctx.trace.stage(f"{stage}:reel"):
        mp4 = await asyncio.to_thread(
            reel.render,
            sources["ground"],
            sources["card"],
            width=w,
            height=h,
            photo_box=tuple(sources["window"]),
        )
    with ctx.trace.stage(f"{stage}:reel_upload"):
        key = r2.key_for(str(ctx.brand_id), str(creative_id), "reel.mp4")
        url = r2.put(key, mp4, "video/mp4")
    return key, url


async def _recompose_one(
    ctx,
    brief,
    slide,
    creative_id,
    background_key,
    brand_snapshot,
    generated: bool = True,
    rebuilt: dict | None = None,
) -> str:
    stage = f"slide{slide.position}"
    if rebuilt is not None:
        image, mime = rebuilt["png"], "image/png"
    else:
        with ctx.trace.stage(f"{stage}:bg_fetch"):
            image = r2.get(background_key)
        mime = "image/jpeg" if background_key.endswith(".jpg") else "image/png"
    focus = None
    if not generated and rebuilt is None:
        # The owner's photograph, whole: cropped around its subject again.
        focus = (await asyncio.to_thread(product.subject_box, image))[0]
    with ctx.trace.stage(f"{stage}:compose"):
        png, report = await compose.compose_with_report(
            brief,
            slide,
            brand_snapshot,
            image,
            mime,
            layout=(rebuilt or {}).get("layout"),
            generated=generated,
            focus=focus,
            subject=(rebuilt or {}).get("subject"),
            keep_sources=brief.is_reel(),
            # A picture carried over was made for the window the OLD copy
            # left; a rebuilt studio was built for the new one. _revision_guard
            # has already proved this trim is inside REUSE_CROP_SHARE.
            kept=generated and rebuilt is None,
        )
        final = await asyncio.to_thread(compose.export_jpeg, png, brief.pixel_size())
    # A revision is looked at exactly as hard as a first version. It is the
    # lane that had NO picture check of any kind, and it is the one the owner
    # already had to ask twice for.
    checked: dict | None = None
    if settings.composite_gate_enabled:
        checked = await _final_check(
            ctx,
            brief,
            slide,
            brand_snapshot,
            creative_id,
            image=image,
            mime=mime,
            png=png,
            final=final,
            report=report,
            generated=generated,
            focus=focus,
            subject=(rebuilt or {}).get("subject"),
            stage=stage,
            lane="recomposed",
        )
        png, final, report = checked["png"], checked["final"], checked["report"]
    with ctx.trace.stage(f"{stage}:upload"):
        key = r2.key_for(str(ctx.brand_id), str(creative_id), "composed.jpg")
        url = r2.put(key, final, "image/jpeg")
    video_key = video_url = None
    if brief.is_reel():
        # A revision of a reel re-renders the motion over the same picture.
        # Still free: no image call.
        video_key, video_url = await _render_reel(ctx, creative_id, report["reel"], stage)
    with session_scope() as db:
        c = db.get(Creative, creative_id)
        c.composed_key, c.composed_url, c.status = key, url, "ready"
        c.video_key, c.video_url = video_key, video_url
        c.timings = dict(ctx.trace.timings)
        if checked is not None:
            c.quality_score, c.template = checked["score"], checked["template"]
            c.cost_micros = int(c.cost_micros or 0) + int(checked["cost_micros"] or 0)
            events.record(
                db,
                kind="quality",
                account_id=ctx.account_id,
                brand_id=ctx.brand_id,
                brief_id=c.brief_id,
                creative_id=creative_id,
                meta={
                    # Splatted first, for the reason _build_one's copy gives.
                    **checked["qa"],
                    "lane": "recomposed",
                    "template": checked["template"],
                    "was_template": checked["was_template"],
                    "score": checked["score"],
                    "measured_score": checked["qa"]["score"],
                    "reasons": checked["reasons"],
                    "inspector_notes": checked["notes"],
                },
            )
    return video_url or url


async def _show(ctx: ToolContext, brief: CreativeBrief, urls: list[str]) -> bool:
    """Send every image (or the reel as a video); True only if every send was
    accepted by the provider."""
    ok = True
    for i, url in enumerate(urls):
        caption = brief.headline if i == 0 else ""
        if brief.is_reel():
            sent = await ctx.show_video(url, caption=caption)
        else:
            sent = await ctx.show(url, caption=caption)
        ok = ok and bool(sent)
    if not ok:
        log.error("creative_not_delivered", account_id=str(ctx.account_id), urls=len(urls))
    return ok


def _stored_pictures(rows) -> dict[int, dict]:
    """What each slide of a stored creative was composed over, by position."""
    return {
        c.slide_position: {
            "template": c.template,
            "provider": c.imagegen_provider or "",
            "background_key": c.background_key or "",
        }
        for c in rows
    }


def cutout_key_beside(background_key: str) -> str:
    """The cut-out stored next to a product-studio background (same day,
    same creative), derived from the background's own key because key_for
    dates a key on the day it is made."""
    return background_key.rsplit("-bg.", 1)[0] + "-cutout.png"


def _owner_photo_slides(rows, slide_position: int | None) -> list[dict]:
    return [
        {"slide": c.slide_position, "lane": c.imagegen_provider}
        for c in sorted(rows, key=lambda c: c.slide_position)
        if (c.imagegen_provider or "") in PHOTO_LANES
        and (slide_position is None or c.slide_position == slide_position)
    ]


_FORMAT_HINT = (
    "Nothing was made and nothing was charged. A post and a story (or reel) are different "
    "shapes, and a picture made for one is never cropped into the other. Use "
    "create_creative for the new format; the owner's photo (if any) is used again for free."
)
_LAYOUT_PICTURE_HINT = (
    "Nothing was made and nothing was charged. The picture on the slide(s) named was made "
    "for the layout it had: in the new layout it would be cut by the window or covered by "
    "the words. Keep the layout and revise the copy, or call regenerate_image with "
    "template_id set to the new layout (1 credit) so a picture is made for its window."
)


async def _revision_guard(
    brief: CreativeBrief, before: dict, snap, layouts: dict[int, dict] | None = None
) -> tuple[dict | None, dict[int, dict]]:
    """A revision must not reuse a picture the new version cannot show.
    Returns (refusal, rebuilt): a refusal to hand back, or the studio pictures
    rebuilt for the new window from the stored cut-out (free: no model runs)
    keyed by slide position.

    Every slide is measured, not only the ones whose TEMPLATE changed. The
    photo window is what the copy leaves, so one more word in the headline
    moves it on split_card and top_band: keyed off the template alone this
    ran on none of the slides an ordinary copy edit touches, and the render
    raised PictureMismatch into _contain_revision's generic
    "generation_failed" -- after the parent brief had already been stamped
    superseded by a version that never rendered.

    A format change (post <-> story/reel) is always refused: the old picture
    is the wrong ratio and would be cropped 30% and enlarged 1.4x. A
    product-studio slide whose window moved is re-placed from its cut-out, in
    the rectangle the NEW copy leaves clear. A generated picture must still
    fit the new window (compose.fits_kept, the same rule the compositor
    applies) and, where the layout changed, keep its subject out from under
    the words. A whole photo is cropped around its subject again by the
    compositor and only its pixels are checked here.

    `layouts` are the reports layout_gate already measured for this draft;
    without them each slide is measured again here.
    """
    old_type = str(before.get("format", {}).get("type") or "single")
    tall = {"story", "reel"}
    if (old_type in tall) != (brief.format.type in tall):
        return {"ok": False, "reason": "format_change_needs_new_picture", "charged": 0,
                "hint": _FORMAT_HINT}, {}  # fmt: skip
    w, h = brief.pixel_size()
    rebuilt: dict[int, dict] = {}
    stuck: list[dict] = []
    for slide in brief.units():
        was = before.get("slides", {}).get(slide.position)
        template = brief.template_for(slide)
        if not was or not was["background_key"]:
            continue
        moved = was["template"] != template
        layout = (layouts or {}).get(slide.position) or await compose.check_layout(
            brief, slide, snap
        )
        window = compose.photo_window(layout, w, h)
        size = (window[2] - window[0], window[3] - window[1])
        image = r2.get(was["background_key"])
        if compose.image_size(image) == size and not moved:
            continue  # the copy left the window exactly where the picture found it
        if was["provider"] == "product_studio":
            try:
                rgba, _ = product.cutout_from_png(r2.get(cutout_key_beside(was["background_key"])))
            except Exception:  # noqa: BLE001 - an older draft without a stored cut-out
                log.warning("cutout_missing_for_revision", position=slide.position)
                stuck.append({"slide": slide.position, "why": "no stored cut-out"})
                continue
            free = _free_rect(layout, window, w, h)
            if product.place_share(rgba.size, size, free) < product.MIN_PLACE_SHARE:
                # The new copy leaves the product a thumbnail's worth of room.
                # Rebuilding it there would hand back a worse card than the one
                # the owner already has, for free, and call it a revision.
                stuck.append({"slide": slide.position, "why": "no room for the product"})
                continue
            png, box = product.studio(
                rgba,
                size[0],
                size[1],
                palette=dict(getattr(snap, "palette", {}) or {}),
                free=free,
                anchor=product.ANCHOR.get(template, 0.5),
            )
            ox, oy = window[0], window[1]
            subject = (box[0] + ox, box[1] + oy, box[2] + ox, box[3] + oy)
            rebuilt[slide.position] = {
                "png": png,
                "subject": subject,
                "layout": layout,
                "cutout": r2.get(cutout_key_beside(was["background_key"])),
            }
            log.info("studio_rebuilt_for_window", position=slide.position, template=template)
            continue
        if was["provider"] in PHOTO_LANES:
            iw, ih = compose.image_size(image)
            if max(size[0] / iw, size[1] / ih) > compose.MAX_PHOTO_UPSCALE:
                stuck.append({"slide": slide.position, "why": "photo too small for the window"})
            continue
        got = compose.image_size(image)
        if not compose.fits_kept(got, size):
            stuck.append({"slide": slide.position, "why": "picture made for another window"})
            continue
        if not moved:
            # The words stayed in the block this layout sets them in, so the
            # picture is still calm where they sit. Running the mask here on
            # every copy edit would put a salient-object pass on the one path
            # the product promises is instant.
            continue
        bbox, trusted = await asyncio.to_thread(product.subject_box, image)
        words = _text_box_in_window(layout, window)
        if trusted and bbox and words:
            scale = size[0] / got[0]
            sl, st, sr, sb = (v * scale / d for v, d in zip(bbox, (*size, *size), strict=True))
            wl, wt, wr, wb = words
            if sl < wr and wl < sr and st < wb and wt < sb:
                stuck.append({"slide": slide.position, "why": "subject under the words"})
    if stuck:
        log.warning("revision_refused_picture", slides=stuck)
        return {"ok": False, "reason": "picture_made_for_other_layout", "charged": 0,
                "slides": stuck, "hint": _LAYOUT_PICTURE_HINT}, {}  # fmt: skip
    return None, rebuilt


def _store_rebuilt(ctx: ToolContext, creative_id: uuid.UUID, rebuilt: dict) -> str:
    key = r2.key_for(str(ctx.brand_id), str(creative_id), "bg.png")
    r2.put(key, rebuilt["png"], "image/png")
    r2.put(cutout_key(str(ctx.brand_id), str(creative_id)), rebuilt["cutout"], "image/png")
    return key


_LEGIBILITY_HINT = (
    "Nothing was sent. The words fit the layout, but on THIS photograph they cannot be made "
    "to read: the plates behind the type are as strong as they go and the contrast is still "
    "short. Shorter copy will not cure it. Offer the owner a layout that sets the words on a "
    "solid panel instead of over the picture (template_id split_card, free), or a different "
    "picture (regenerate_image, 1 credit)."
)


def _contain_revision(
    pairs: list[tuple[Slide, uuid.UUID, str, str]], results: list
) -> dict[str, Any] | None:
    """The tool result for a revision whose render was refused, or None.

    The layout gate measures on a blank background, so it cannot see what
    compose measures on the real photograph: type that will not separate from
    what is behind it (compose.LegibilityError), or a browser that went away.
    Such a refusal used to escape recompose as a raw exception -- the agent got
    a stack trace, the owner got silence, and the new Creative rows stayed
    'composing' for ever. A revision is one answer to one request, so one
    refused slide refuses the whole version: nothing is sent, the slides that
    raised are marked failed, and the version they already have is untouched.
    """
    failures: list[str] = []
    legibility = True
    quality = True
    hint = ""
    for (slide, cid, _, _), res in zip(pairs, results, strict=True):
        if not isinstance(res, BaseException):
            continue
        log.error(
            "revision_slide_failed",
            creative_id=str(cid),
            position=slide.position,
            error=str(res)[:300],
        )
        _mark_failed(cid, str(res), cost_micros=getattr(res, "cost_micros", 0))
        failures.append(f"slide {slide.position}: {res}")
        legibility = legibility and isinstance(res, compose.LegibilityError)
        quality = quality and isinstance(res, CompositeRejected)
        hint = hint or getattr(res, "hint", "")
    if not failures:
        return None
    if legibility:
        return {
            "ok": False,
            "reason": "legibility",
            "errors": failures[:3],
            "hint": _LEGIBILITY_HINT,
        }
    if quality:
        # A revision never buys a picture, so the final check refusing one is
        # the end of the line for THIS arrangement -- and the agent can do
        # something about that, if it is told what. "generation_failed" with a
        # stack trace is not something anybody can act on.
        return {
            "ok": False,
            "reason": "composite_quality",
            "errors": failures[:3],
            "hint": hint,
        }
    return {"ok": False, "reason": "generation_failed", "errors": failures[:3]}


def _mark_failed(creative_id: uuid.UUID, error: str, *, cost_micros: int = 0) -> None:
    with session_scope() as db:
        c = db.get(Creative, creative_id)
        if c is not None:
            c.status, c.error = "failed", error[:2000]
            if cost_micros:
                c.cost_micros = int(cost_micros)


def _refund(ctx: ToolContext, group_id: uuid.UUID, units: int, reason: str) -> None:
    if units <= 0:
        return
    with session_scope() as db:
        credits.refund(
            db,
            account_id=ctx.account_id,
            amount=credits.cost_of("generate_creative") * units,
            reason=reason,
            idempotency_key=f"refund:{group_id}:{reason}",
        )


def _merge(payload: dict, changes: dict) -> dict:
    """Shallow merge, one level deep for nested objects the agent may touch."""
    merged = dict(payload)
    for key, value in changes.items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


# What a free revision can change: every field the brief expresses, and only
# those. "badge", "price" or "logo_size" are wishes nothing renders; a version
# that silently ignored one cost the owner a second change request to find out.
REVISABLE = frozenset(CreativeBrief.model_fields)
DIFF_VALUE_LIMIT = 120


def unsupported_changes(changes: dict) -> list[str]:
    return sorted(k for k in changes if k not in REVISABLE)


def _short(value: Any) -> Any:
    """A diff value the event row and the agent can read: strings and nested
    objects are cut at DIFF_VALUE_LIMIT, everything else is kept as it is."""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, str) and len(value) > DIFF_VALUE_LIMIT:
        return value[: DIFF_VALUE_LIMIT - 1] + "…"
    return value


def payload_diff(old: dict, new: dict) -> dict[str, list]:
    """{field: [old, new]} for every brief field a revision actually changed.

    Compared on the VALIDATED payloads, so a headline that only differs in
    stray whitespace is the same headline. Nested objects (caption, format,
    visual_direction) and carousel slides are compared one field at a time,
    so the agent can say "slide 2's headline" rather than quote a list.
    """
    diff: dict[str, list] = {}

    def fields(prefix: str, a: dict, b: dict) -> None:
        for f in sorted(set(a) | set(b)):
            if a.get(f) != b.get(f):
                diff[f"{prefix}{f}"] = [_short(a.get(f)), _short(b.get(f))]

    for key in sorted(set(old) | set(new)):
        a, b = old.get(key), new.get(key)
        if a == b:
            continue
        if key == "slides" and isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
            for sa, sb in zip(a, b, strict=True):
                pos = sb.get("position") or sa.get("position")
                fields(f"slides[{pos}].", sa, sb)
        elif isinstance(a, dict) and isinstance(b, dict):
            fields(f"{key}.", a, b)
        else:
            diff[key] = [_short(a), _short(b)]
    return diff


def _remember_change(
    db, *, brand_id: uuid.UUID, kind: str, owner_request: str | None, fields: list[str], brief_id
) -> None:
    """The owner's reason, in memory, linked to the version it produced -- the
    way a tap's vote is remembered in insights/votes. It used to reach memory
    only if the model chose to call `remember`, and then without the brief.
    Best effort: a memory that fails must never fail the revision."""
    if not owner_request:
        return
    try:
        from app.memory import embed

        with db.begin_nested():
            embed.remember(
                db,
                brand_id=brand_id,
                kind=kind,
                content=f"Owner asked: {owner_request} -> changed {', '.join(fields) or 'nothing'}",
                source_ref=f"brief:{brief_id}",
            )
    except Exception:  # noqa: BLE001 - memory is an enhancement, never a gate
        log.warning("change_memory_failed", kind=kind, brief_id=str(brief_id))


class BrandAssetSnapshot:
    __slots__ = ("id", "storage_key", "mime", "kind", "label")

    def __init__(
        self,
        id: str,
        storage_key: str,
        mime: str | None,
        kind: str = "product",
        label: str | None = None,
    ) -> None:
        self.id, self.storage_key, self.mime, self.kind = id, storage_key, mime, kind
        self.label = label


def _resolve_photos(
    db, brand_id: uuid.UUID, brief: CreativeBrief, units: list[Slide]
) -> dict[int, str]:
    """Slide position -> brand_assets.id for every slide that should use a real photo.

    An explicit reference always wins. For the rest, the owner's own photographs
    are matched against this slide's copy, and a slide with no genuine match is
    left to the image model rather than given a photograph of the wrong product.

    Within one carousel a photo is used at most once: two slides showing the
    same jar reads as a bug, not as a set.
    """
    candidates = list(
        db.scalars(
            select(BrandAsset)
            .where(
                BrandAsset.brand_id == brand_id,
                BrandAsset.kind.notin_(("logo", "reference")),
            )
            .order_by(BrandAsset.created_at.desc())
            .limit(60)
        )
    )
    known = {str(c.id) for c in candidates}

    # An explicit reference is honoured only if it is a real photo of THIS
    # brand, and one the compositor can use: the same floor the automatic
    # match applies (no logo, not under 800px), because a referenced 640px
    # photo was enlarged to the canvas and shipped soft.
    by_id = {str(c.id): c for c in candidates}
    out: dict[int, str] = {}
    for u in units:
        raw = u.visual_direction.reference_asset_id
        if raw and str(raw) in known and photoref.is_usable(by_id[str(raw)]):
            out[u.position] = str(raw)
        elif raw:
            log.warning("reference_asset_ignored", position=u.position, asset_id=str(raw))
    open_slides = [u for u in units if u.position not in out]
    if not open_slides or not candidates:
        return out

    taken = set(out.values())
    for u in open_slides:
        pick = photoref.choose(
            photoref.copy_text(u, brief),
            [c for c in candidates if str(c.id) not in taken],
            direction=photoref.direction_text(u),
        )
        if pick is not None:
            out[u.position] = str(pick)
            taken.add(str(pick))
    auto = sorted(p for p in out if p in {u.position for u in open_slides})
    if auto:
        log.info("real_photo_matched", brand_id=str(brand_id), slides=auto)
    return out


def _load_assets(
    db,
    brand_id: uuid.UUID,
    units: list[Slide],
    resolved: dict[int, str] | None = None,
) -> dict[str, BrandAssetSnapshot]:
    ids = {v for v in (resolved or {}).values() if v}
    if not ids:
        return {}
    out: dict[str, BrandAssetSnapshot] = {}
    for raw in ids:
        try:
            asset = db.get(BrandAsset, uuid.UUID(raw))
        except ValueError:
            continue
        if asset is not None and asset.brand_id == brand_id:
            out[raw] = BrandAssetSnapshot(
                raw, asset.storage_key, asset.mime, asset.kind, asset.label
            )
    return out


class _BrandSnapshot:
    """Detached copy so compositing does not hold a DB session open."""

    __slots__ = (
        "name",
        "category",
        "logo_url",
        "logo_src",
        "logo_analysis",
        "palette",
        "fonts",
        "never_say",
        "template_prefs",
    )

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


def _snapshot(db, brand: Brand) -> _BrandSnapshot:
    """Inline the logo as a data URI, once per request.

    The compositor renders in a headless browser. A remote <img> means every
    creative depends on a network fetch completing inside the render, and when
    it does not the creative ships with a hole where the brand mark should be --
    silently, because a missing image is not an error. Fetching the bytes once
    here and embedding them removes that failure mode entirely, and is faster
    than one fetch per slide.
    """
    logo_src = None
    asset = db.scalar(
        select(BrandAsset)
        .where(BrandAsset.brand_id == brand.id, BrandAsset.kind == "logo")
        .order_by(BrandAsset.created_at.desc())
        .limit(1)
    )
    if asset is not None:
        try:
            logo_src = compose.as_data_uri(r2.get(asset.storage_key), asset.mime or "image/png")
        except Exception:  # noqa: BLE001 - the brand name is a fine fallback
            log.warning("logo_inline_failed", brand_id=str(brand.id), key=asset.storage_key)
    return _BrandSnapshot(
        name=brand.name,
        category=brand.category,
        logo_url=brand.logo_url,
        logo_src=logo_src or brand.logo_url,
        logo_analysis=dict(brand.logo_analysis or {}),
        palette=dict(brand.palette or {}),
        fonts=dict(brand.fonts or {}),
        never_say=list(brand.never_say or []),
        template_prefs=dict(brand.template_prefs or {}),
    )
