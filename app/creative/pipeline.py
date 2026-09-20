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
  * each slide is sent the moment it is ready, not held for the slowest one;
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
    shotplan,
)
from app.creative.brief import CreativeBrief, Slide, check_brand_rules
from app.creative.imagegen import ImageRequest, get_provider
from app.creative.imagegen.base import generation_size
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
    """Sends each slide the moment it is ready, and says so when the job is slow.

    Slides finish in whatever order the vendor returns them, so every carousel
    image is captioned with its place ("3/6") and the owner can see the order
    even when 3 lands before 2.
    """

    def __init__(self, ctx: ToolContext, brief: CreativeBrief, total: int, locale: str) -> None:
        self.ctx, self.brief, self.total = ctx, brief, total
        self.lang = ((locale or "en").split("-")[0]).lower()
        self.sent: dict[int, bool] = {}
        self._lock = asyncio.Lock()
        self._watch: asyncio.Task | None = None

    def caption(self, position: int) -> str:
        if self.total <= 1:
            return self.brief.headline
        return f"{position}/{self.total}" + (f" · {self.brief.headline}" if position == 1 else "")

    async def send(self, position: int, url: str) -> bool:
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
            line = table.get(self.lang, table["en"]).format(done=len(self.sent), total=self.total)
            log.info("slow_notice", elapsed_s=elapsed, done=len(self.sent), total=self.total)
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
    "clipped": "a word is wider than the layout",
    "outside": "the copy runs off the canvas",
    "unsafe": "the copy reaches into the strip the profile grid trims",
    "overlap": "two elements would overlap",
    "logo_clearspace": "the copy crowds the logo",
    "logo_not_loaded": "the logo could not be loaded",
}


async def layout_gate(brief: CreativeBrief, units: list[Slide], brand_snapshot) -> dict | None:
    """The deterministic guarantees, checked before any money moves.

    Returns the tool result to hand back when a slide cannot be set, or None.
    There is no degraded render to fall back to: the copy gets shorter.
    """
    results = await asyncio.gather(
        *(compose.check_layout(brief, s, brand_snapshot) for s in units),
        return_exceptions=True,
    )
    problems = []
    for slide, res in zip(units, results, strict=True):
        if isinstance(res, compose.LayoutError):
            kinds = list(dict.fromkeys(v.split(":", 1)[0] for v in res.violations))
            problems.append(
                {
                    "slide": slide.position,
                    "headline": slide.headline,
                    "problems": [_LAYOUT_WORDS.get(k, k) for k in kinds],
                    "detail": res.violations[:6],
                }
            )
        elif isinstance(res, BaseException):
            raise res
    if not problems:
        return None
    log.warning("layout_gate_refused", slides=[p["slide"] for p in problems])
    return {
        "ok": False,
        "reason": "copy_does_not_fit",
        "charged": 0,
        "slides": problems,
        "hint": (
            "Nothing was made and nothing was charged. At a readable size this copy cannot be "
            "set without cropping or overlapping, and we never ship that. Shorten the headline, "
            "subhead or CTA on the slide(s) named -- fewer words, not smaller words -- and call "
            "the tool again. Do not tell the owner about layout; just send the tighter version."
        ),
    }


async def generate(
    ctx: ToolContext,
    brief: CreativeBrief,
    *,
    reuse: dict[int, tuple[str, str]] | None = None,
) -> dict:
    """Generate every slide of a brief.

    `reuse` maps slide position -> (background_key, background_url) for slides
    whose picture is being KEPT. That is how regenerating one slide of a
    carousel charges for one slide: the others are re-composited over the
    background they already have, exactly like a copy revision.
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

        # A product photo and centred type fight for the same pixels. When the
        # owner's product is in the picture the words move to the lower third
        # and the product gets the top. Decided HERE -- before the slides are
        # snapshotted and the brief stored -- so the render, the stored brief,
        # a later free revision, and the taste/grid history all agree.
        template_switched = False
        if brief.template_id == "centered_overlay" and any(
            (a := assets.get(resolved.get(u.position, ""))) and a.kind == "product" for u in units
        ):
            brief.template_id = product.PREFERRED_TEMPLATE
            for sl in brief.slides:
                if sl.template_id == "centered_overlay":
                    sl.template_id = None
            units = brief.units()
            template_switched = True
        brand_snapshot = _snapshot(db, brand)

    # Prove the copy can be set -- no overlap, no crop, inside the safe zone,
    # the mark clear -- BEFORE the brief is stored or a credit is charged. The
    # layout does not depend on the picture, so there is no reason to buy one
    # first and find out afterwards.
    unfit = await layout_gate(brief, units, brand_snapshot)
    if unfit:
        return unfit

    with session_scope() as db:
        billable_positions = {
            u.position for u in units if not resolved.get(u.position) and u.position not in reuse
        }
        billable = len(billable_positions)

        brief_row = repo.save_brief(
            db,
            account_id=ctx.account_id,
            brand_id=ctx.brand_id,
            payload=brief.model_dump(mode="json"),
            source_message_id=ctx.message_id,
        )
        brief_id = brief_row.id
        events.record(
            db,
            kind="created",
            account_id=ctx.account_id,
            brand_id=ctx.brand_id,
            brief_id=brief_id,
            meta={
                **events.facts_of(brief_row.payload or {}),
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
                )
                for slide, cid in zip(units, creative_ids, strict=True)
            ),
            return_exceptions=True,
        )
    finally:
        await delivery.stop()
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

    # Already sent, one by one, as each slide finished (see _Delivery).
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
        "note": "The owner can see it now. Ask if they want changes; keep it to one line.",
    }
    if failures:
        out["failed_slides"] = failures[:6]
        out["note"] = (
            f"{len(ok_urls)} of {len(units)} slides were made and sent; the rest could not be made "
            "to our standard and were refunded (see failed_slides). We never send a picture "
            "that failed the check. Tell the owner plainly, in one line, which slide is missing "
            "and offer to try that slide again with regenerate_image."
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
    if template_switched:
        out["template"] = brief.template_id
        out["template_note"] = (
            "Their product photo is in this creative, so the words were set in the lower "
            "third to keep the product clear. Do not mention this unless they ask."
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


async def recompose(ctx: ToolContext, *, brief_id: uuid.UUID, changes: dict) -> dict:
    """Copy-only revision. Reuses every stored background: no image call, no charge."""
    # The new copy has to be settable before anything is stored. Done in its own
    # short session so the browser work does not hold a pooled connection.
    draft, snap = None, None
    with session_scope() as db:
        parent, brand = db.get(Brief, brief_id), db.get(Brand, ctx.brand_id)
        if parent is not None and brand is not None:
            try:
                draft = CreativeBrief.model_validate(_merge(parent.payload, changes))
                snap = _snapshot(db, brand)
            except Exception:  # noqa: BLE001 - reported properly by the validation below
                draft = None
    if draft is not None:
        unfit = await layout_gate(draft, draft.units(), snap)
        if unfit:
            return unfit

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

        merged = _merge(parent.payload, changes)
        try:
            brief = CreativeBrief.model_validate(merged)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": "invalid_changes", "error": str(exc)[:400]}

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
            parent=parent,
        )
        new_brief_id = new_brief.id
        events.record(
            db,
            kind="revise",
            account_id=ctx.account_id,
            brand_id=ctx.brand_id,
            brief_id=parent.id,
            meta={**events.facts_of(parent.payload or {}), "changed": sorted(changes)},
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
            pairs.append((slide, c.id, old.background_key))
        brand_snapshot = _snapshot(db, brand)

    urls = await asyncio.gather(
        *(_recompose_one(ctx, brief, slide, cid, key, brand_snapshot) for slide, cid, key in pairs)
    )
    await _show(ctx, brief, list(urls))
    return {
        "ok": True,
        "brief_id": str(new_brief_id),
        "creative_ids": [str(cid) for _, cid, _ in pairs],
        "image_urls": list(urls),
        "shown_to_user": True,
        "credits_charged": 0,
        "note": "Copy-only revision: same backgrounds reused, nothing charged.",
    }


async def regenerate_image(
    ctx: ToolContext,
    *,
    brief_id: uuid.UUID,
    new_prompt: str | None,
    slide_position: int | None = None,
) -> dict:
    with session_scope() as db:
        parent = db.get(Brief, brief_id)
        if parent is None:
            return {"ok": False, "reason": "unknown_brief"}
        payload = dict(parent.payload)

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
                        reuse[c.slide_position] = (c.background_key, c.background_url or "")
    with session_scope() as db:
        events.record(
            db,
            kind="regenerate",
            account_id=ctx.account_id,
            brand_id=ctx.brand_id,
            brief_id=brief_id,
            meta={**events.facts_of(payload), "slide_position": slide_position},
        )
    return await generate(ctx, brief, reuse=reuse)


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
):
    """Generate, inspect, and regenerate until a picture passes -- or fail.

    Every attempt is the SAME call at the SAME settings; only the prompt gains
    a sentence about what was wrong, and the seed moves. Reasons to reject:
    anything bggate.inspect reports, a size other than the one asked for, or a
    picture an earlier slide of this carousel already produced. Every rejection
    is logged with its reason and attempt number. On exhaustion this raises:
    the slide is failed and refunded, and the owner is told.
    """
    attempts = max(1, int(settings.imagegen_gate_attempts))
    gw, gh = size
    sem = (register or {}).get("sem") or asyncio.Semaphore(1)
    # The mock draws a gradient for tests and local development; there is no
    # model output to inspect. Every real vendor is inspected, no exceptions.
    inspected = provider.name != "mock"
    cost, rejections, reasons_so_far = 0, [], []
    for attempt in range(1, attempts + 1):
        last = attempt == attempts
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

        reasons: list[str] = []
        notes = ""
        got = await asyncio.to_thread(compose.image_size, res.data)
        if getattr(provider, "exact_size", False) and got != (gw, gh):
            reasons.append("wrong_size")
            notes = f"asked {gw}x{gh}, got {got[0]}x{got[1]}"
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
    seen = sorted({r for rej in rejections for r in rej["reasons"]})
    raise BackgroundRejected(
        f"no acceptable picture in {attempts} attempts ({', '.join(seen)})",
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
) -> str:
    w, h = brief.pixel_size()
    ref = (resolved or {}).get(slide.position)
    stage = f"slide{slide.position}"
    job_id, cost_micros = None, 0  # set only when a vendor was paid
    gate: dict | None = None  # set only when a picture was generated and inspected

    if reuse and slide.position in reuse:
        # Picture kept from the previous version of this creative.
        with ctx.trace.stage(f"{stage}:reuse"):
            key = reuse[slide.position][0]
            image, mime = r2.get(key), ("image/jpeg" if key.endswith(".jpg") else "image/png")
        provider_name = "reused"
    elif ref and ref in assets:
        # The owner's own photograph. No model call, no charge.
        with ctx.trace.stage(f"{stage}:asset_fetch"):
            image, mime = r2.get(assets[ref].storage_key), assets[ref].mime or "image/jpeg"
        provider_name = "brand_asset"
        if assets[ref].kind == "product":
            # Cut the product out and stand it in a studio. Refused cuts fall
            # back to the photo as it was -- a wrong cut ships a broken product.
            with ctx.trace.stage(f"{stage}:product_cutout"):
                studio = await asyncio.to_thread(
                    product.product_background,
                    image,
                    w,
                    h,
                    palette=dict(getattr(brand_snapshot, "palette", {}) or {}),
                    template=brief.template_for(slide),
                )
            if studio is not None:
                image, mime = studio[0], "image/jpeg"
                provider_name = "product_studio"
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
        )
        # Generated natively at 4:5 ABOVE the delivery size and resampled down
        # by the compositor. Never generated at a preset and cropped.
        res, cost_micros, rejections = await _generate_checked(
            ctx,
            provider,
            brief,
            slide,
            creative_id,
            prompt,
            negative,
            generation_size((w, h)),
            register,
            stage,
        )
        image, mime, job_id = res.data, res.mime, res.job_id
        gate = {"attempts": len(rejections) + 1, "rejections": rejections, "raw": res.raw}

    with ctx.trace.stage(f"{stage}:compose"):
        png = await compose.compose(brief, slide, brand_snapshot, image, mime)
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
        composed_key = r2.key_for(str(ctx.brand_id), str(creative_id), "composed.jpg")
        composed_url = r2.put(composed_key, final, "image/jpeg")

    video_key = video_url = None
    if brief.is_reel():
        video_key, video_url = await _render_reel(ctx, creative_id, image, png, stage)

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


async def _render_reel(ctx, creative_id: uuid.UUID, photo: bytes, card: bytes, stage: str):
    """The card set in motion: a seven-second MP4 beside the still, in R2.

    CPU-bound (PIL frames piped to ffmpeg), so it runs in a thread and never
    blocks the other slides or the WhatsApp loop.
    """
    from app.creative import reel

    w, h = 1080, 1920
    with ctx.trace.stage(f"{stage}:reel"):
        mp4 = await asyncio.to_thread(reel.render, photo, card, width=w, height=h)
    with ctx.trace.stage(f"{stage}:reel_upload"):
        key = r2.key_for(str(ctx.brand_id), str(creative_id), "reel.mp4")
        url = r2.put(key, mp4, "video/mp4")
    return key, url


async def _recompose_one(ctx, brief, slide, creative_id, background_key, brand_snapshot) -> str:
    stage = f"slide{slide.position}"
    with ctx.trace.stage(f"{stage}:bg_fetch"):
        image = r2.get(background_key)
    mime = "image/jpeg" if background_key.endswith(".jpg") else "image/png"
    with ctx.trace.stage(f"{stage}:compose"):
        png = await compose.compose(brief, slide, brand_snapshot, image, mime)
        final = await asyncio.to_thread(compose.export_jpeg, png, brief.pixel_size())
    with ctx.trace.stage(f"{stage}:upload"):
        key = r2.key_for(str(ctx.brand_id), str(creative_id), "composed.jpg")
        url = r2.put(key, final, "image/jpeg")
    video_key = video_url = None
    if brief.is_reel():
        # A revision of a reel -- or a still turned into one -- re-renders the
        # motion over the same photograph. Still free: no image call.
        video_key, video_url = await _render_reel(ctx, creative_id, image, png, stage)
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


class BrandAssetSnapshot:
    __slots__ = ("id", "storage_key", "mime", "kind")

    def __init__(self, id: str, storage_key: str, mime: str | None, kind: str = "product") -> None:
        self.id, self.storage_key, self.mime, self.kind = id, storage_key, mime, kind


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
            .where(BrandAsset.brand_id == brand_id, BrandAsset.kind != "logo")
            .order_by(BrandAsset.created_at.desc())
            .limit(60)
        )
    )
    known = {str(c.id) for c in candidates}

    # An explicit reference is honoured only if it is a real photo of THIS
    # brand. The model authors that field; a made-up or foreign id used to skip
    # billing and then fall through to a paid model call anyway.
    out: dict[int, str] = {}
    for u in units:
        raw = u.visual_direction.reference_asset_id
        if raw and str(raw) in known:
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
            out[raw] = BrandAssetSnapshot(raw, asset.storage_key, asset.mime, asset.kind)
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
