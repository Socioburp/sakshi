"""Tool definitions and dispatch.

The brief schema handed to the model is generated from `CreativeBrief` itself,
so the contract cannot drift between the validator and the prompt. `$defs` are
inlined because a flat schema is what the tools API wants.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.agent.context import ToolContext
from app.billing import credits
from app.creative import pipeline
from app.creative.brief import CreativeBrief, Grounding
from app.db import repo
from app.db.models import Brand, BrandAsset, Brief, Creative, IgAccount, Publication
from app.db.session import session_scope
from app.integrations.instagram import client as ig
from app.logging import get_logger
from app.memory import embed as memory_embed
from app.memory import retrieve as memory_retrieve

log = get_logger(__name__)


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].rsplit("/", 1)[-1]
                merged = walk(copy.deepcopy(defs.get(name, {})))
                extra = {k: v for k, v in node.items() if k != "$ref"}
                return {**merged, **extra}
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


BRIEF_SCHEMA = _inline_refs(CreativeBrief.model_json_schema())

TOOLS: list[dict[str, Any]] = [
    {
        "name": "create_creative",
        "description": (
            "Turn the owner's request into a finished creative and send it to them on "
            "WhatsApp. Costs the owner 1 credit. Call this as soon as you know what is "
            "being promoted -- do not gather every detail first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"brief": BRIEF_SCHEMA},
            "required": ["brief"],
        },
    },
    {
        "name": "revise_creative",
        "description": (
            "Change the words on an existing creative and re-send it. Keeps the same "
            "background image, so it is fast and FREE. Use this for headline, subhead, "
            "CTA, badge, price, template, caption or hashtag changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "brief_id": {
                    "type": "string",
                    "description": "brief_id returned by a previous tool result",
                },
                "changes": {
                    "type": "object",
                    "description": "Only the fields that change. Same shape as the brief.",
                    "properties": {
                        "headline": {"type": "string", "maxLength": 60},
                        "subhead": {"type": "string", "maxLength": 100},
                        "cta": {"type": "string", "maxLength": 30},
                        "alt_text": {"type": "string", "maxLength": 300},
                        "caption": {
                            "type": "object",
                            "properties": {
                                "body": {"type": "string"},
                                "hashtags": {"type": "array", "items": {"type": "string"}},
                                "language": {"type": "string"},
                            },
                        },
                        "template_id": {
                            "type": "string",
                            "enum": ["centered_overlay", "lower_third", "split_card"],
                        },
                        "slides": {
                            "type": "array",
                            "description": (
                                "Carousel only. Send every slide, not just the changed ones."
                            ),
                            "items": {"type": "object"},
                        },
                    },
                },
            },
            "required": ["brief_id", "changes"],
        },
    },
    {
        "name": "regenerate_image",
        "description": (
            "Generate a NEW background photograph for an existing creative, keeping the "
            "copy. Costs 1 credit. Only use this when the owner objects to the picture "
            "itself, not to the words."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "brief_id": {"type": "string"},
                "new_prompt": {
                    "type": "string",
                    "description": (
                        "Optional replacement visual direction. Background imagery only -- "
                        "no text, letters, logos or signs."
                    ),
                },
                "slide_position": {
                    "type": "integer",
                    "description": (
                        "Carousel only: which slide's picture to redo. "
                        "Omit for a single post."
                    ),
                },
            },
            "required": ["brief_id"],
        },
    },
    {
        "name": "update_brand",
        "description": (
            "Save a durable fact about the brand: name, colours, tone, audience, or a "
            "phrase they never want used. Call this when the owner tells you something "
            "that will still be true next month."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "category": {"type": "string"},
                "tagline": {"type": "string"},
                "description": {"type": "string"},
                "target_audience": {"type": "string"},
                "tone": {"type": "string"},
                "languages": {"type": "array", "items": {"type": "string"}},
                "never_say": {"type": "array", "items": {"type": "string"}},
                "always_say": {"type": "array", "items": {"type": "string"}},
                "palette": {
                    "type": "object",
                    "properties": {
                        "primary": {"type": "string"},
                        "secondary": {"type": "string"},
                        "accent": {"type": "string"},
                        "ink": {"type": "string"},
                    },
                },
            },
        },
    },
    {
        "name": "remember",
        "description": (
            "Store something for later recall. `product` builds their catalogue -- name, "
            "price, what it is. `rejection` records something they turned down AND why; "
            "call it every time they say no, it is what stops you repeating the mistake. "
            "`style_anchor` marks a creative they were clearly pleased with. Use "
            "update_brand for core identity instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": [
                        "product", "style_anchor", "rejection", "past_creative",
                        "feedback", "campaign", "fact", "note",
                    ],
                },
                "content": {"type": "string"},
            },
            "required": ["kind", "content"],
        },
    },
    {
        "name": "recall",
        "description": "Search what you have stored for this brand.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "connect_instagram",
        "description": (
            "Get the link the owner taps to connect their Instagram account. Send them "
            "the link; they finish it in the browser."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "request_approval",
        "description": (
            "Ask the client to approve a creative for Instagram. Sends them tappable "
            "buttons. ALWAYS call this before publishing -- you cannot publish without "
            "their recorded tap, no matter what they say in chat."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "brief_id": {"type": "string"},
                "message": {
                    "type": "string",
                    "description": "One short line asking if it can go up. Their language.",
                },
            },
            "required": ["brief_id"],
        },
    },
    {
        "name": "publish_to_instagram",
        "description": (
            "Publish a creative the owner has approved. Only call after they have "
            "explicitly said yes to this specific creative."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "brief_id": {
                    "type": "string",
                    "description": "Publishes every slide of this brief as one post.",
                },
                "creative_id": {"type": "string", "description": "Alternative to brief_id."},
                "caption": {"type": "string", "description": "Overrides the brief caption."},
            },
        },
    },
    {
        "name": "list_brand_assets",
        "description": (
            "List real photos the owner has sent -- product shots, the shop, the team. "
            "Use an id in visual_direction.reference_asset_id when the post should show "
            "the actual product instead of a generated stand-in. Those slides are free."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "check_credits",
        "description": (
            "Check how many creative credits the owner has left. Use before promising "
            "work you may not be able to deliver."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


# --------------------------------------------------------------------------- #
async def dispatch(ctx: ToolContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"ok": False, "reason": "unknown_tool", "tool": name}
    with ctx.trace.stage(f"tool:{name}"):
        try:
            return await handler(ctx, args)
        except Exception as exc:  # noqa: BLE001
            log.exception("tool_failed", tool=name)
            return {"ok": False, "reason": "tool_error", "error": str(exc)[:300]}


async def _create_creative(ctx: ToolContext, args: dict) -> dict:
    try:
        brief = CreativeBrief.model_validate(args["brief"])
    except Exception as exc:  # noqa: BLE001
        # Handed straight back to the model, which usually fixes it on the next turn.
        return {"ok": False, "reason": "invalid_brief", "error": str(exc)[:800]}

    # Overwrite whatever the model put in `grounding` with what retrieval
    # actually returned. The field exists to debug retrieval quality, and a
    # model-authored version records what it believed rather than what it was
    # given -- which is exactly backwards when a creative comes out wrong.
    brief.grounding = Grounding.model_validate(ctx.grounding.as_brief_grounding())
    return await pipeline.generate(ctx, brief)


async def _revise_creative(ctx: ToolContext, args: dict) -> dict:
    return await pipeline.recompose(
        ctx, brief_id=uuid.UUID(args["brief_id"]), changes=args.get("changes", {})
    )


async def _regenerate_image(ctx: ToolContext, args: dict) -> dict:
    return await pipeline.regenerate_image(
        ctx,
        brief_id=uuid.UUID(args["brief_id"]),
        new_prompt=args.get("new_prompt"),
        slide_position=args.get("slide_position"),
    )


async def _update_brand(ctx: ToolContext, args: dict) -> dict:
    settable = {
        "name", "category", "tagline", "description", "target_audience", "tone",
        "languages", "never_say", "always_say", "palette",
    }
    with session_scope() as db:
        brand = db.get(Brand, ctx.brand_id)
        changed = []
        for key, value in args.items():
            if key not in settable or value in (None, "", [], {}):
                continue
            if key in ("never_say", "always_say", "languages"):
                merged = list(dict.fromkeys([*(getattr(brand, key) or []), *value]))
                setattr(brand, key, merged)
            elif key == "palette":
                brand.palette = {**(brand.palette or {}), **value}
            else:
                setattr(brand, key, value)
            changed.append(key)
    return {"ok": True, "updated": changed}


async def _remember(ctx: ToolContext, args: dict) -> dict:
    with session_scope() as db:
        memory_embed.remember(
            db, brand_id=ctx.brand_id, kind=args["kind"], content=args["content"]
        )
    return {"ok": True, "stored": args["content"][:120]}


async def _recall(ctx: ToolContext, args: dict) -> dict:
    with session_scope() as db:
        hits = memory_retrieve.search(
            db, brand_id=ctx.brand_id, query=args["query"], k=int(args.get("k", 5))
        )
        return {
            "ok": True,
            "results": [
                {"kind": m.kind, "content": m.content, "similarity": round(s, 3)}
                for m, s in hits
            ],
        }


async def _connect_instagram(ctx: ToolContext, args: dict) -> dict:
    url = ig.authorize_url(state=str(ctx.account_id))
    return {
        "ok": True,
        "connect_url": url,
        "note": "Send this link to the owner in your reply. Do not describe the steps.",
    }


async def _request_approval(ctx: ToolContext, args: dict) -> dict:
    from app.channels.base import Button

    brief_id = args["brief_id"]
    with session_scope() as db:
        rows = repo.creatives_for_brief(db, uuid.UUID(brief_id))
        if not rows:
            return {"ok": False, "reason": "unknown_brief"}
        if not all(r.status in ("ready", "approved") for r in rows):
            return {"ok": False, "reason": "creative_not_ready"}
        slides = len(rows)

    text = args.get("message") or (
        "Post this to Instagram?" if slides == 1 else f"Post these {slides} slides to Instagram?"
    )
    await ctx.say(
        text,
        buttons=[
            Button(id=f"approve:{brief_id}", title="Post to Instagram"),
            Button(id=f"revise:{brief_id}", title="Change something"),
        ],
    )
    return {
        "ok": True,
        "awaiting": "client_tap",
        "note": (
            "Buttons sent. Stop here and wait -- do not call publish_to_instagram until "
            "they have actually tapped. Say nothing further this turn."
        ),
    }


async def _publish_to_instagram(ctx: ToolContext, args: dict) -> dict:
    """Publish an approved creative. Single post or carousel, same entry point.

    Instagram's carousel publish is two-level: one container per slide, then a
    parent CAROUSEL container holding their ids, then publish the parent. The
    child ids are persisted before the parent call -- if the process dies in
    between, orphaned containers you cannot identify are unrecoverable.
    """
    brief_id = args.get("brief_id")
    creative_id = args.get("creative_id")

    with session_scope() as db:
        if brief_id:
            creatives = repo.creatives_for_brief(db, uuid.UUID(brief_id))
        else:
            one = db.get(Creative, uuid.UUID(creative_id))
            creatives = repo.creatives_for_brief(db, one.brief_id) if one else []
        creatives = [c for c in creatives if c.status in ("ready", "approved")]
        if not creatives:
            return {"ok": False, "reason": "creative_not_ready"}

        # The gate. `approved_at` is written at ingest from the client's own tap,
        # so no amount of conversational persuasion reaches this branch.
        unapproved = [c for c in creatives if c.approved_at is None]
        if unapproved:
            return {
                "ok": False,
                "reason": "not_approved_by_client",
                "approved": len(creatives) - len(unapproved),
                "total": len(creatives),
                "hint": (
                    "Call request_approval and wait for them to tap. Their typed 'yes' "
                    "is not enough -- publishing needs the recorded approval."
                ),
            }

        brief_row = db.get(Brief, creatives[0].brief_id)
        brief = CreativeBrief.model_validate(brief_row.payload)
        ig_row = db.scalar(
            select(IgAccount)
            .where(IgAccount.brand_id == ctx.brand_id, IgAccount.status == "connected")
            .limit(1)
        )
        if ig_row is None:
            return {
                "ok": False,
                "reason": "instagram_not_connected",
                "hint": "Call connect_instagram and send the owner the link.",
            }

        caption = args.get("caption") or brief.caption.rendered() or brief.headline
        is_carousel = len(creatives) > 1
        pub = Publication(
            creative_id=creatives[0].id,
            ig_account_id=ig_row.id,
            media_type="CAROUSEL" if is_carousel else "IMAGE",
            caption=caption,
            hashtags=brief.caption.hashtags,
            alt_text=brief.alt_text,
            status="creating_container",
        )
        db.add(pub)
        db.flush()
        pub_id, token, ig_user_id = pub.id, ig_row.access_token, ig_row.ig_user_id
        urls = [c.composed_url for c in creatives]
        creative_ids = [c.id for c in creatives]

    try:
        if is_carousel:
            children = await asyncio.gather(
                *(
                    ig.create_media_container(
                        ig_user_id=ig_user_id,
                        access_token=token,
                        image_url=url,
                        is_carousel_item=True,
                    )
                    for url in urls
                )
            )
            with session_scope() as db:
                db.get(Publication, pub_id).child_container_ids = list(children)
            for child in children:
                await ig.wait_for_container(container_id=child, access_token=token)
            container_id = await ig.create_carousel_container(
                ig_user_id=ig_user_id,
                access_token=token,
                children=list(children),
                caption=caption,
            )
        else:
            container_id = await ig.create_media_container(
                ig_user_id=ig_user_id,
                access_token=token,
                image_url=urls[0],
                caption=caption,
                alt_text=brief.alt_text,
            )
        await ig.wait_for_container(container_id=container_id, access_token=token)
        result = await ig.publish_container(
            ig_user_id=ig_user_id, access_token=token, container_id=container_id
        )
    except Exception as exc:  # noqa: BLE001
        with session_scope() as db:
            p = db.get(Publication, pub_id)
            p.status, p.error = "failed", str(exc)[:2000]
        return {"ok": False, "reason": "publish_failed", "error": str(exc)[:200]}

    with session_scope() as db:
        p = db.get(Publication, pub_id)
        p.ig_container_id = container_id
        p.ig_media_id = result.media_id
        p.permalink = result.permalink
        p.status = "published"
        p.published_at = datetime.now(UTC)
        for cid in creative_ids:
            db.get(Creative, cid).status = "published"
    return {
        "ok": True,
        "permalink": result.permalink,
        "media_id": result.media_id,
        "slides": len(urls),
    }


async def _check_credits(ctx: ToolContext, args: dict) -> dict:
    from app.db.models import Account

    with session_scope() as db:
        acct = db.get(Account, ctx.account_id)
        return {"ok": True, "credits": acct.credits_balance, "plan": acct.plan}


async def _list_brand_assets(ctx: ToolContext, args: dict) -> dict:
    with session_scope() as db:
        rows = db.scalars(
            select(BrandAsset)
            .where(BrandAsset.brand_id == ctx.brand_id)
            .order_by(BrandAsset.created_at.desc())
            .limit(25)
        ).all()
        return {
            "ok": True,
            "assets": [
                {"id": str(a.id), "kind": a.kind, "label": a.label} for a in rows
            ],
        }


_HANDLERS = {
    "create_creative": _create_creative,
    "request_approval": _request_approval,
    "revise_creative": _revise_creative,
    "regenerate_image": _regenerate_image,
    "update_brand": _update_brand,
    "remember": _remember,
    "recall": _recall,
    "connect_instagram": _connect_instagram,
    "publish_to_instagram": _publish_to_instagram,
    "list_brand_assets": _list_brand_assets,
    "check_credits": _check_credits,
}

# Tools that already put something in front of the owner. After one of these
# runs, a long explanatory reply is noise -- the picture is the reply.
SHOWS_MEDIA = {"create_creative", "revise_creative", "regenerate_image"}
_ = credits  # re-exported for callers that charge outside the pipeline
