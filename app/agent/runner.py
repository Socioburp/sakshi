"""The agent loop.

One agentic pipeline, not a chain of classifiers. The model sees the whole
conversation, the whole brand identity, and the tools; it decides whether this
turn is a question, a new creative, or a revision. That is the v2 decision --
v1's intent router is gone.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from anthropic import AsyncAnthropic

from app.agent import language as lang
from app.agent.context import ToolContext
from app.agent.prompts import ONBOARDING_HINT, WINDOW_CLOSING_HINT, build_system
from app.agent.tools import SHOWS_MEDIA, TOOLS, dispatch
from app.channels.base import Button
from app.channels.whatsapp.session_window import seconds_left
from app.config import settings
from app.db import repo
from app.db.models import Account, Brand, Message, WaSession
from app.db.session import session_scope
from app.logging import get_logger
from app.memory import grounding as memory_grounding
from app.telemetry.stages import Trace

log = get_logger(__name__)

_client: AsyncAnthropic | None = None
HISTORY_LIMIT = 24


def get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        if not settings.anthropic_model:
            raise RuntimeError(
                "ANTHROPIC_MODEL is unset. Copy the exact model id from "
                "console.anthropic.com into .env -- do not guess it."
            )
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


def _text_of(msg: Message) -> str:
    if msg.kind == "audio":
        return msg.transcript or "[voice note, could not be transcribed]"
    if msg.kind == "image":
        return f"[sent a photo]{(' ' + msg.text) if msg.text else ''}"
    return msg.text or f"[{msg.kind}]"


def _covered(rows: list[Message], message: Message) -> list[uuid.UUID]:
    """The inbound messages this turn is answering: every unanswered inbound
    row after the last outbound one (they fold into a single user turn),
    plus the message itself."""
    ids: list[uuid.UUID] = []
    for m in rows:
        if m.direction == "out":
            ids = []
        elif m.answered_at is None:
            ids.append(m.id)
    if message.id not in ids:
        ids.append(message.id)
    return ids


def _mark_answered(ids: list[uuid.UUID]) -> None:
    if not ids:
        return
    try:
        with session_scope() as db:
            for m in db.query(Message).filter(Message.id.in_(ids), Message.answered_at.is_(None)):
                m.answered_at = datetime.now(UTC)
    except Exception:  # noqa: BLE001 - bookkeeping; the reply already went out
        log.exception("mark_answered_failed")


def _history(rows: list[Message]) -> list[dict[str, Any]]:
    """Collapse the message log into alternating turns."""
    out: list[dict[str, Any]] = []
    for m in rows:
        role = "user" if m.direction == "in" else "assistant"
        content = _text_of(m)
        if not content:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n" + content
        else:
            out.append({"role": role, "content": content})
    while out and out[0]["role"] != "user":
        out.pop(0)
    # A burst of two messages: the first job answers both (they collapse into
    # one user turn), so the second job's history ends with that assistant
    # reply. Sending that as the final turn asks the API to CONTINUE the
    # reply -- a fragment or a repeated, charged tool call. Drop it.
    while out and out[-1]["role"] != "user":
        out.pop()
    return out


async def run_turn(*, message_id: uuid.UUID, trace: Trace) -> dict[str, Any]:
    with session_scope() as db:
        message = db.get(Message, message_id)
        if message is None:
            return {"ok": False, "reason": "unknown_message"}
        account_id = message.account_id
        session_id = message.session_id
        brand = repo.default_brand(db, account_id)
        if brand is None:
            brand = Brand(account_id=account_id, name="My brand")
            db.add(brand)
            db.flush()
        brand_id = brand.id
        wa_session = db.get(WaSession, session_id) if session_id else None
        wa_id = wa_session.wa_id if wa_session else ""
        history_rows = (
            repo.recent_messages(db, session_id, HISTORY_LIMIT) if session_id else [message]
        )
        # A burst: two messages arrive, the first job answers both (they
        # collapse into one user turn), then the second job runs. Answering
        # again would repeat the reply -- and repeat a charged tool call. The
        # answering turn stamps every inbound it covered (see _covered below),
        # so this is a recorded fact, not an inference from row order.
        if message.answered_at is not None:
            log.info("turn_skipped_already_answered", message_id=str(message_id))
            return {"ok": True, "reason": "already_answered", "tools": [], "media": []}
        covered = _covered(history_rows, message)
        is_first = len(history_rows) <= 1
        window_left = seconds_left(wa_session)
        latest_text = _text_of(message)

        memory_block = ""
        grounded = memory_grounding.Grounded()
        if latest_text and not is_first:
            with trace.stage("grounding"):
                grounded = memory_grounding.ground(db, brand_id=brand_id, query=latest_text)
            memory_block = grounded.as_prompt_block()

        # Language and script are two facts. A typed message tells us both. A
        # transcript tells us the language only -- its script is the STT
        # vendor's choice, not the owner's. A photo or a button tap tells us
        # nothing. So: lock the language from text or audio, lock the script
        # from text only, and when this message carries no signal answer the
        # way the owner has been answered so far.
        typed = message.kind == "text"
        has_words = message.kind in ("text", "audio")
        detected = lang.detect(latest_text if has_words else "")
        account = db.get(Account, account_id)
        if detected.confidence >= 0.6 and account is not None:
            locale = lang.to_locale(detected.language)
            changed = account.locale != locale
            account.locale = locale
            if typed:
                changed = changed or account.script != detected.script
                account.script = detected.script
            if changed:
                log.info(
                    "language_locked",
                    account_id=str(account_id),
                    language=detected.language,
                    script=account.script,
                    source=message.kind,
                    confidence=round(detected.confidence, 2),
                )
        if typed and detected.confidence >= 0.6:
            profile = detected
        else:
            profile = lang.from_locale(
                account.locale if account else None,
                script=account.script if account else None,
                fallback=detected,
            )

        # The brief_id a tool returned last turn is gone from the model's
        # context by the next message. Without this block, "change the headline"
        # or an approval tap gave the model nothing to call revise/publish with,
        # and it either invented an id or made (and charged) a new creative.
        current = _current_brief_block(db, wa_session)
        taste_block = _taste_block(db, brand_id)
        perf_block = _performance_block(db, brand_id)
        pending = _pending_block(wa_session)
        plan_block = _plan_block(db, brand_id)

        system = build_system(
            brand,
            memory_block,
            language_block=lang.instruction(profile),
            extra="\n\n".join(
                x
                for x in (
                    ONBOARDING_HINT if is_first else "",
                    WINDOW_CLOSING_HINT if 0 < window_left < 3600 else "",
                    current,
                    taste_block,
                    perf_block,
                    plan_block,
                    pending,
                )
                if x
            ),
        )
        messages = _history(history_rows)

    ctx = ToolContext(
        account_id=account_id,
        brand_id=brand_id,
        session_id=session_id,
        wa_id=wa_id,
        trace=trace,
        message_id=message_id,
        grounding=grounded,
    )
    trace.account_id = account_id

    used_tools: list[str] = []
    try:
        result = await _loop(ctx, system, messages, used_tools, trace, message_id, profile)
        _mark_answered(covered)
        return result
    except Exception as exc:  # noqa: BLE001
        # Whatever broke -- the model API, a tool, the database -- the owner
        # must not be left staring at a blue tick. One line, in their language,
        # and the job is reported failed so it is visible in the logs.
        log.exception("turn_failed", message_id=str(message_id), tools=used_tools)
        try:
            await ctx.say(lang.sorry_line(profile))
            _mark_answered(covered)  # they got a reply, even if it was an apology
        except Exception:  # noqa: BLE001
            log.exception("turn_failed_and_apology_failed", message_id=str(message_id))
        return {"ok": False, "reason": "turn_failed", "error": str(exc)[:300]}


def _current_brief_block(db, wa_session: WaSession | None) -> str:
    if wa_session is None or not wa_session.active_brief_id:
        return ""
    rows = repo.creatives_for_brief(db, wa_session.active_brief_id)
    if not rows:
        return ""
    approved = all(r.approved_at for r in rows)
    status = (
        "approved by the owner (their tap), publish_to_instagram will work"
        if approved
        else "NOT yet approved -- publish will refuse until they tap"
    )
    return (
        f"## Current creative\n"
        f"brief_id: {wa_session.active_brief_id}\n"
        f"slides: {len(rows)} -- status: {status}\n"
        "Use this brief_id for revise_creative, regenerate_image, request_approval and "
        "publish_to_instagram. Do not create a new creative when they are asking for a "
        "change to this one."
    )


def _taste_block(db, brand_id: uuid.UUID) -> str:
    """What this owner has approved before, when there is enough of it to trust."""
    try:
        from app.insights.profile import taste

        return taste(db, brand_id).as_prompt_block()
    except Exception:  # noqa: BLE001 - advice, never a gate
        log.exception("taste_block_failed", brand_id=str(brand_id))
        return ""


def _performance_block(db, brand_id: uuid.UUID) -> str:
    """What their followers responded to, once enough posts have numbers."""
    try:
        from app.insights import performance

        return performance.build(db, brand_id).as_prompt_block()
    except Exception:  # noqa: BLE001 - advice, never a gate
        log.exception("performance_block_failed", brand_id=str(brand_id))
        return ""


def _plan_block(db, brand_id: uuid.UUID) -> str:
    """This month's plan, compressed: goal, cadence, the next two slots."""
    try:
        from zoneinfo import ZoneInfo

        from app.insights import plan as planning

        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        row = planning.current(db, brand_id, today)
        if row is None:
            return ""
        d = planning.describe(row)
        upcoming = [s for s in row.slots if s.get("status") == "planned"][:2]
        nxt = "; ".join(f"{s['date'][5:]}: {s['headline_idea']}" for s in upcoming) or "none left"
        return (
            f"## This month's plan\nGoal: {d['goal']}, {d['cadence_per_week']} posts/week, "
            f"{d['posts_made']}/{d['posts_planned']} made. Next: {nxt}."
        )
    except Exception:  # noqa: BLE001 - context, never a gate
        log.exception("plan_block_failed", brand_id=str(brand_id))
        return ""


def _pending_block(wa_session: WaSession | None) -> str:
    """Suggestions or a grid choice offered last turn, so a tap means something."""
    if wa_session is None or not wa_session.state:
        return ""
    state = wa_session.state
    lines = []
    if ideas := state.get("suggestions"):
        lines.append("## Ideas you offered last turn (their tap or reply refers to these)")
        for i in ideas:
            lines.append(
                f"- #{i.get('rank')}: {i.get('headline_idea')} -- {i.get('format')}, "
                f"{i.get('template')}, {i.get('aspect_ratio')}; visual: {i.get('visual_direction')}"
                + (
                    f"; reference_asset_id={i['reference_asset_id']}"
                    if i.get("reference_asset_id")
                    else ""
                )
            )
        lines.append(
            "Their tap 'Make it' / yes = build #1 with create_creative (set suggestion_rank). "
            "'Another idea' = offer #2 in one line. 'Not today' = drop it, no follow-up. "
            "(Button titles appear in their language.)"
        )
    if pend := state.get("grid_choice"):
        orig, adj = pend.get("original") or {}, pend.get("adjusted") or {}
        o_fmt, a_fmt = orig.get("format") or {}, adj.get("format") or {}
        o_vd, a_vd = orig.get("visual_direction") or {}, adj.get("visual_direction") or {}
        diffs = [
            f"{label}: {a} -> {b}"
            for label, a, b in (
                ("ratio", o_fmt.get("aspect_ratio"), a_fmt.get("aspect_ratio")),
                ("layout", orig.get("template_id"), adj.get("template_id")),
                ("mood", o_vd.get("mood"), a_vd.get("mood")),
            )
            if a != b
        ]
        lines.append("## Grid choice you offered last turn (both briefs are kept server-side)")
        lines.append(
            f"Headline: {orig.get('headline', '')!r}. "
            f"Adjusted version changes: {'; '.join(diffs) or 'nothing'}."
        )
        lines.append(
            "Their tap 'Match my grid' = create_creative(grid_choice='adjusted'); "
            "'As I said' = create_creative(grid_choice='original'). Send no brief."
        )
    return "\n".join(lines)


def _remember_brief(session_id: uuid.UUID | None, result: dict) -> None:
    brief_id = result.get("brief_id") if isinstance(result, dict) else None
    if not session_id or not brief_id:
        return
    with session_scope() as db:
        sess = db.get(WaSession, session_id)
        if sess is not None:
            sess.active_brief_id = uuid.UUID(str(brief_id))


async def _loop(ctx, system, messages, used_tools, trace, message_id, profile) -> dict[str, Any]:
    client = get_client()
    # A tool may attach reply buttons ("Make it / Another idea / Not today").
    # They ride on the model's final line for this turn; the model cannot
    # send buttons itself and must not be asked to describe them.
    pending_buttons: list[Button] = []
    for turn in range(settings.agent_max_turns):
        with trace.stage(f"model:{turn}"):
            resp = await client.messages.create(
                model=settings.anthropic_model,
                max_tokens=settings.agent_max_tokens,
                system=system,
                tools=TOOLS,
                messages=messages,
            )

        assistant_blocks = [b.model_dump() for b in resp.content]
        messages.append({"role": "assistant", "content": assistant_blocks})

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            if text:
                await ctx.say(text, buttons=pending_buttons or None)
            return {
                "ok": True,
                "reply": text,
                "tools": used_tools,
                "media": ctx.sent_media,
                "turns": turn + 1,
            }

        results = []
        for tu in tool_uses:
            used_tools.append(tu.name)
            result = await dispatch(ctx, tu.name, tu.input or {})
            if tu.name in SHOWS_MEDIA and result.get("ok"):
                _remember_brief(ctx.session_id, result)
            if isinstance(result.get("buttons"), list):
                pending_buttons = [
                    Button(id=str(b["id"]), title=str(b["title"])[:20])
                    for b in result["buttons"]
                    if isinstance(b, dict) and b.get("id") and b.get("title")
                ][:3]
                result["reminder"] = (
                    "The image is already on the owner's phone. Reply with at most one "
                    "short line -- do not describe the creative."
                )
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": __import__("json").dumps(result, ensure_ascii=False)[:6000],
                    "is_error": not result.get("ok", True),
                }
            )
        messages.append({"role": "user", "content": results})

    log.warning("agent_max_turns", message_id=str(message_id), tools=used_tools)
    await ctx.say(lang.sorry_line(profile))
    return {"ok": False, "reason": "max_turns", "tools": used_tools}
