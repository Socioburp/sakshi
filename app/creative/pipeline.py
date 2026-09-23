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
    dedupe,
    photoreal,
    photoref,
    product,
    remembered,
    shotplan,
)
from app.creative.brief import CreativeBrief, Slide, check_brand_rules
from app.creative.imagegen import ImageRequest, get_provider
from app.creative.imagegen.base import generation_size_for_window
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
_WORKING: dict[str, str] = {
    "hi": "Bana raha hoon… {secs} second.",
    "kn": "Maadta iddini… {secs} second.",
    "ta": "Panren… {secs} second.",
    "te": "Chestunna… {secs} second.",
    "mr": "Banavtoy… {secs} second.",
    "ml": "Cheyyunnu… {secs} second.",
    "en": "Making it… about {secs} seconds.",
}


def working_line(locale: str | None, slides: int) -> str:
    lang = ((locale or "en").split("-")[0]).lower()
    # NOT MEASURED YET at gpt-image-2 / high / 1600x2000 -- OpenAI documents "up
    # to 2 minutes" per image. Set from the recorded p50 once there is one.
    return _WORKING.get(lang, _WORKING["en"]).format(secs=90 if slides <= 1 else 120)


_STILL_WORKING: dict[str, str] = {
    "hi": "Abhi ban raha hai… {done}/{total} taiyaar. Achhi quality mein thoda time lagta hai.",
    "en": "Still working… {done} of {total} ready. The high-quality pictures take a little longer.",
}
_STILL_WORKING_ONE: dict[str, str] = {
    "hi": "Abhi ban raha hai… achhi quality mein thoda time lagta hai.",
    "en": "Still working on it… the high-quality picture takes a little longer.",
}
# Notices go out this many seconds-multiples after the start: 1x, 3x, 7x of
# SLOW_NOTICE_S. Three at most -- WhatsApp is not a log.
_NOTICE_AT = (1, 3, 7)


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

    async def _notices(self) -> None:
        elapsed = 0
        for mult in _NOTICE_AT:
            wait = settings.slow_notice_s * mult - elapsed
            await asyncio.sleep(wait)
            elapsed += wait
            table = _STILL_WORKING_ONE if self.total <= 1 else _STILL_WORKING
            done = len(self.ready)
            line = table.get(self.lang, table["en"]).format(done=done, total=self.total)
            log.info("slow_notice", elapsed_s=elapsed, done=done, total=self.total)
            try:
                await self.ctx.progress(line)
            except Exception:  # noqa: BLE001 - a missed notice must never cost the creative
                log.warning("slow_notice_failed")


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
    units = brief.units()

    with session_scope() as db:
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

    if failures and not ok_urls:
        _refund(ctx, group_id, billable, "creative_failed")
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
            "template_switches was changed to keep the words off the subject (or to show a "
            "small photo without enlarging it). Do not mention this unless they ask."
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
    unfit = await layout_gate(draft, draft.units(), snap)
    if unfit:
        return unfit
    # A revision never reuses a picture made for another shape or layout.
    refused, rebuilt = await _revision_guard(draft, before, snap)
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
) -> dict:
    with session_scope() as db:
        parent = db.get(Brief, brief_id)
        if parent is None:
            return {"ok": False, "reason": "unknown_brief"}
        # A deep copy: the slide dicts below are rewritten in place, and the
        # parent's payload is what the diff on the event is measured against.
        parent_payload = copy.deepcopy(parent.payload)
        parent_version = parent.version
        payload = copy.deepcopy(parent.payload)
        own = _owner_photo_slides(repo.creatives_for_brief(db, brief_id), slide_position)
    if own:
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
        if slide_position is not None:
            # Keep every other slide's picture. Without this, redoing slide 3 of
            # six re-bought all six -- and replaced five the owner already liked.
            now = datetime.now(UTC)
            with session_scope() as db:
                prev = repo.creatives_for_brief(db, brief_id)
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
                    if c.slide_position != slide_position and c.background_key:
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
    slow part, and it used to run inside the render of every slide)."""
    plans: dict[int, PhotoPlan] = {}
    by_asset: dict[str, PhotoPlan] = {}
    for u in units:
        ref = resolved.get(u.position)
        if not ref or ref not in assets:
            continue
        if ref not in by_asset:
            by_asset[ref] = await asyncio.to_thread(_read_photo, assets[ref])
        plans[u.position] = by_asset[ref]
    return plans


def _set_template(brief: CreativeBrief, slide: Slide, template: str) -> None:
    """Change one slide's layout in the brief itself -- units() rebuilds a
    single post's slide from the brief, so the slide object alone is not it."""
    if brief.is_carousel():
        slide.template_id = template
    else:
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


# How far a non-exact vendor's frame may stray from the ratio asked for: a
# 0.5% difference is under a pixel of trim at the window; the 896x1088 that a
# "4:5" once came back as is 2.9%, and it was silently cropped and enlarged.
SIZE_RATIO_TOLERANCE = 0.005


def _size_fault(provider, got: tuple[int, int], asked: tuple[int, int], floor) -> str:
    """Why `got` is not the frame that was asked for, or '' when it is."""
    if getattr(provider, "exact_size", False):
        return "" if got == asked else f"asked {asked[0]}x{asked[1]}, got {got[0]}x{got[1]}"
    want = asked[0] / asked[1]
    if abs(got[0] / got[1] - want) / want > SIZE_RATIO_TOLERANCE:
        return f"ratio {got[0]}x{got[1]} is not {asked[0]}x{asked[1]}"
    need = floor or asked
    if got[0] < need[0] or got[1] < need[1]:
        return f"{got[0]}x{got[1]} is below the {need[0]}x{need[1]} it must cover"
    return ""


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
):
    """Generate, inspect, and regenerate until a picture passes -- or fail.

    `floor` is the window the picture must cover (never enlarged to). A
    vendor that picks its own size from a ratio (replicate, bfl) is held to
    the ratio within SIZE_RATIO_TOLERANCE and to the floor; one that returns
    exactly what it is asked is held to exactly that.

    Every attempt is the SAME call at the SAME settings; only the prompt gains
    a sentence about what was wrong, and the seed moves. Reasons to reject:
    anything bggate.inspect reports, a size other than the one asked for, or a
    picture an earlier slide of this carousel already produced. Every rejection
    is logged with its reason and attempt number. On exhaustion this raises:
    the slide is failed and refunded, and the owner is told.
    """
    attempts = max(1, int(settings.imagegen_gate_attempts))
    budget = int(settings.imagegen_gate_budget_micros or 0)
    gw, gh = size
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
                verdict = await bggate.inspect(res.data)
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
        with ctx.trace.stage(f"{stage}:reuse"):
            key = reuse[slide.position][0]
            image, mime = r2.get(key), ("image/jpeg" if key.endswith(".jpg") else "image/png")
        provider_name = "reused"
        kept = reuse[slide.position]
        generated = (kept[2] if len(kept) > 2 else "") not in PHOTO_LANES
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
        )
        # PNG all the way to here; this is the single lossy encode. It also
        # refuses any frame that is not exactly the post size.
        final = await asyncio.to_thread(compose.export_jpeg, png, (w, h))

    with ctx.trace.stage(f"{stage}:upload"):
        if provider_name == "reused":
            # The background already lives in R2 under its old key; a copy per
            # revision is storage for nothing.
            bg_key = reuse[slide.position][0]
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
        )
        final = await asyncio.to_thread(compose.export_jpeg, png, brief.pixel_size())
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
    "template_id set in the revision first (1 credit) so a picture is made for the new "
    "layout."
)


async def _revision_guard(
    brief: CreativeBrief, before: dict, snap
) -> tuple[dict | None, dict[int, dict]]:
    """A revision that changes the shape or the layout must not reuse a
    picture made for the old one. Returns (refusal, rebuilt): a refusal to
    hand back, or the studio pictures rebuilt for the new window from the
    stored cut-out (free: no model runs) keyed by slide position.

    A format change (post <-> story/reel) is always refused: the old picture
    is the wrong ratio and would be cropped 30% and enlarged 1.4x. On a
    layout change, a product-studio slide is re-placed from its cut-out; a
    generated picture must still fit the new window and keep its subject
    out from under the words, or the revision is refused; a whole photo is
    cropped around its subject again by the compositor and only its pixels
    are checked here.
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
        if not was or was["template"] == template or not was["background_key"]:
            continue
        layout = await compose.check_layout(brief, slide, snap)
        window = compose.photo_window(layout, w, h)
        size = (window[2] - window[0], window[3] - window[1])
        if was["provider"] == "product_studio":
            try:
                rgba, _ = product.cutout_from_png(r2.get(cutout_key_beside(was["background_key"])))
            except Exception:  # noqa: BLE001 - an older draft without a stored cut-out
                log.warning("cutout_missing_for_revision", position=slide.position)
                stuck.append({"slide": slide.position, "why": "no stored cut-out"})
                continue
            free = _free_rect(layout, window, w, h)
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
            log.info("studio_rebuilt_for_layout", position=slide.position, template=template)
            continue
        image = r2.get(was["background_key"])
        if was["provider"] in PHOTO_LANES:
            iw, ih = compose.image_size(image)
            if max(size[0] / iw, size[1] / ih) > compose.MAX_PHOTO_UPSCALE:
                stuck.append({"slide": slide.position, "why": "photo too small for the window"})
            continue
        from app.creative.imagegen.base import crop_for

        got = compose.image_size(image)
        if crop_for(got, size) > compose.GENERATED_CROP_TOLERANCE or got[0] < size[0]:
            stuck.append({"slide": slide.position, "why": "picture made for another window"})
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
    for (slide, cid, _, _), res in zip(pairs, results, strict=True):
        if not isinstance(res, BaseException):
            continue
        log.error(
            "revision_slide_failed",
            creative_id=str(cid),
            position=slide.position,
            error=str(res)[:300],
        )
        _mark_failed(cid, str(res))
        failures.append(f"slide {slide.position}: {res}")
        legibility = legibility and isinstance(res, compose.LegibilityError)
    if not failures:
        return None
    if legibility:
        return {
            "ok": False,
            "reason": "legibility",
            "errors": failures[:3],
            "hint": _LEGIBILITY_HINT,
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
