"""Brief -> pixels -> WhatsApp.

Three entry points, and the difference between them is the whole cost model:

  generate     charge per image call, generate, composite, upload, send
  recompose    copy-only change: reuse the stored backgrounds, composite again.
               No image call, no charge.
  regenerate   new background, same copy. Charges again.

A carousel is N creatives sharing one `carousel_group_id`. Slides are dispatched
IN PARALLEL -- a 5-slide carousel that generated serially would take most of a
minute, which on WhatsApp reads as the bot having died.

A slide whose `visual_direction.reference_asset_id` is set skips generation
entirely and composites over the owner's own photograph. That path is free.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.agent.context import ToolContext
from app.billing import credits
from app.creative import compose, photoreal, photoref
from app.creative.brief import CreativeBrief, Slide, check_brand_rules
from app.creative.imagegen import ImageRequest, get_provider
from app.db import repo
from app.db.models import Brand, BrandAsset, Brief, Creative
from app.db.session import session_scope
from app.integrations.storage import r2
from app.logging import get_logger

log = get_logger(__name__)

DRAFT_TTL_DAYS = 7


class CreativeFailed(Exception):
    pass


# --------------------------------------------------------------------------- #
async def generate(ctx: ToolContext, brief: CreativeBrief) -> dict:
    units = brief.units()
    group_id = uuid.uuid4()

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

        # Real photograph first. Any slide the agent left unreferenced gets the
        # owner's own photo when one genuinely matches -- decided here, before
        # the charge, so the free lane is actually free.
        resolved = _resolve_photos(db, ctx.brand_id, brief, units)
        billable = sum(1 for u in units if not resolved.get(u.position))

        brief_row = repo.save_brief(
            db,
            account_id=ctx.account_id,
            brand_id=ctx.brand_id,
            payload=brief.model_dump(mode="json"),
            source_message_id=ctx.message_id,
        )
        brief_id = brief_row.id

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
        brand_snapshot = _snapshot(db, brand)
        assets = _load_assets(db, ctx.brand_id, units, resolved)

    # Slides run concurrently. gather with return_exceptions so one bad slide
    # does not discard the ones that already succeeded.
    results = await asyncio.gather(
        *(
            _build_one(ctx, brief, slide, cid, brand_snapshot, assets, resolved)
            for slide, cid in zip(units, creative_ids, strict=True)
        ),
        return_exceptions=True,
    )

    ok_urls: list[str] = []
    failures: list[str] = []
    for slide, cid, res in zip(units, creative_ids, results, strict=True):
        if isinstance(res, BaseException):
            log.exception("slide_failed", creative_id=str(cid), position=slide.position)
            _mark_failed(cid, str(res))
            failures.append(f"slide {slide.position}: {res}")
        else:
            ok_urls.append(res)

    if failures and not ok_urls:
        _refund(ctx, group_id, billable, "creative_failed")
        return {"ok": False, "reason": "generation_failed", "errors": failures[:3]}
    if failures:
        # Partial carousel: refund only what did not ship.
        _refund(ctx, group_id, len(failures), "partial_carousel")

    await _show(ctx, brief, ok_urls)
    return {
        "ok": True,
        "brief_id": str(brief_id),
        "creative_ids": [str(c) for c in creative_ids],
        "carousel_group_id": str(group_id) if brief.is_carousel() else None,
        "slides_ok": len(ok_urls),
        "slides_failed": len(failures),
        "image_urls": ok_urls,
        "shown_to_user": True,
        "credits_charged": billable - len(failures),
        "note": "The owner can see it now. Ask if they want changes; keep it to one line.",
    }


async def recompose(ctx: ToolContext, *, brief_id: uuid.UUID, changes: dict) -> dict:
    """Copy-only revision. Reuses every stored background: no image call, no charge."""
    with session_scope() as db:
        parent = db.get(Brief, brief_id)
        if parent is None:
            return {"ok": False, "reason": "unknown_brief"}
        brand = db.get(Brand, ctx.brand_id)
        prev = repo.creatives_for_brief(db, brief_id)
        if not prev or not all(c.background_key for c in prev):
            return {"ok": False, "reason": "no_background_to_reuse"}

        merged = _merge(parent.payload, changes)
        try:
            brief = CreativeBrief.model_validate(merged)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": "invalid_changes", "error": str(exc)[:400]}

        violations = check_brand_rules(brief, brand)
        if violations:
            return {"ok": False, "reason": "never_say_violation", "violations": violations}

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
        *(
            _recompose_one(ctx, brief, slide, cid, key, brand_snapshot)
            for slide, cid, key in pairs
        )
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
        if slide_position and payload.get("slides"):
            for s in payload["slides"]:
                if s["position"] == slide_position:
                    s["visual_direction"] = {**s["visual_direction"], "prompt": new_prompt}
                    s["visual_direction"].pop("seed", None)
                    break
        else:
            payload["visual_direction"] = {**payload["visual_direction"], "prompt": new_prompt}
            payload["visual_direction"].pop("seed", None)

    try:
        brief = CreativeBrief.model_validate(payload)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "invalid_visual_direction", "error": str(exc)[:400]}
    return await generate(ctx, brief)


# --------------------------------------------------------------------------- #
async def _build_one(
    ctx: ToolContext,
    brief: CreativeBrief,
    slide: Slide,
    creative_id: uuid.UUID,
    brand_snapshot,
    assets: dict[str, BrandAssetSnapshot],
    resolved: dict[int, str] | None = None,
) -> str:
    w, h = brief.pixel_size()
    ref = (resolved or {}).get(slide.position) or slide.visual_direction.reference_asset_id
    stage = f"slide{slide.position}"

    if ref and ref in assets:
        # The owner's own photograph. No model call, no charge.
        with ctx.trace.stage(f"{stage}:asset_fetch"):
            image, mime = r2.get(assets[ref].storage_key), assets[ref].mime or "image/jpeg"
        provider_name = "brand_asset"
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
        )
        with ctx.trace.stage(f"{stage}:imagegen", provider=provider.name):
            res = await provider.generate(
                ImageRequest(
                    prompt=prompt,
                    negative=negative,
                    width=w,
                    height=h,
                    seed=slide.visual_direction.seed,
                    style=slide.visual_direction.mood,
                )
            )
        image, mime = res.data, res.mime

    with ctx.trace.stage(f"{stage}:compose"):
        png = await compose.compose(brief, slide, brand_snapshot, image, mime)

    with ctx.trace.stage(f"{stage}:upload"):
        ext = "jpg" if "jpeg" in mime else "png"
        bg_key = r2.key_for(str(ctx.brand_id), str(creative_id), f"bg.{ext}")
        r2.put(bg_key, image, mime)
        composed_key = r2.key_for(str(ctx.brand_id), str(creative_id), "composed.png")
        composed_url = r2.put(composed_key, png, "image/png")

    with session_scope() as db:
        c = db.get(Creative, creative_id)
        c.background_key, c.background_url = bg_key, r2.public_url(bg_key)
        c.composed_key, c.composed_url = composed_key, composed_url
        c.imagegen_provider = provider_name
        c.status = "ready"
        c.timings = dict(ctx.trace.timings)
    return composed_url


async def _recompose_one(ctx, brief, slide, creative_id, background_key, brand_snapshot) -> str:
    with ctx.trace.stage(f"slide{slide.position}:bg_fetch"):
        image = r2.get(background_key)
    with ctx.trace.stage(f"slide{slide.position}:compose"):
        png = await compose.compose(brief, slide, brand_snapshot, image, "image/jpeg")
    with ctx.trace.stage(f"slide{slide.position}:upload"):
        key = r2.key_for(str(ctx.brand_id), str(creative_id), "composed.png")
        url = r2.put(key, png, "image/png")
    with session_scope() as db:
        c = db.get(Creative, creative_id)
        c.composed_key, c.composed_url, c.status = key, url, "ready"
        c.timings = dict(ctx.trace.timings)
    return url


async def _show(ctx: ToolContext, brief: CreativeBrief, urls: list[str]) -> None:
    for i, url in enumerate(urls):
        await ctx.show(url, caption=brief.headline if i == 0 else "")


def _mark_failed(creative_id: uuid.UUID, error: str) -> None:
    with session_scope() as db:
        c = db.get(Creative, creative_id)
        if c is not None:
            c.status, c.error = "failed", error[:2000]


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
    __slots__ = ("id", "storage_key", "mime")

    def __init__(self, id: str, storage_key: str, mime: str | None) -> None:
        self.id, self.storage_key, self.mime = id, storage_key, mime


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
    out = {
        u.position: str(u.visual_direction.reference_asset_id)
        for u in units
        if u.visual_direction.reference_asset_id
    }
    open_slides = [u for u in units if u.position not in out]
    if not open_slides:
        return out

    candidates = list(
        db.scalars(
            select(BrandAsset)
            .where(BrandAsset.brand_id == brand_id, BrandAsset.kind != "logo")
            .order_by(BrandAsset.created_at.desc())
            .limit(60)
        )
    )
    if not candidates:
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
    ids = {
        u.visual_direction.reference_asset_id
        for u in units
        if u.visual_direction.reference_asset_id
    }
    ids |= {v for v in (resolved or {}).values() if v}
    if not ids:
        return {}
    out: dict[str, BrandAssetSnapshot] = {}
    for raw in ids:
        try:
            asset = db.get(BrandAsset, uuid.UUID(raw))
        except ValueError:
            continue
        if asset is not None and asset.brand_id == brand_id:
            out[raw] = BrandAssetSnapshot(raw, asset.storage_key, asset.mime)
    return out


class _BrandSnapshot:
    """Detached copy so compositing does not hold a DB session open."""

    __slots__ = (
        "name",
        "logo_url",
        "logo_src",
        "logo_analysis",
        "palette",
        "fonts",
        "never_say",
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
        logo_url=brand.logo_url,
        logo_src=logo_src or brand.logo_url,
        logo_analysis=dict(brand.logo_analysis or {}),
        palette=dict(brand.palette or {}),
        fonts=dict(brand.fonts or {}),
        never_say=list(brand.never_say or []),
    )
