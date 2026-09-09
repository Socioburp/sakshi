"""The agent loop.

One agentic pipeline, not a chain of classifiers. The model sees the whole
conversation, the whole brand identity, and the tools; it decides whether this
turn is a question, a new creative, or a revision. That is the v2 decision --
v1's intent router is gone.
"""

from __future__ import annotations

import uuid
from typing import Any

from anthropic import AsyncAnthropic

from app.agent import language as lang
from app.agent.context import ToolContext
from app.agent.prompts import ONBOARDING_HINT, WINDOW_CLOSING_HINT, build_system
from app.agent.tools import SHOWS_MEDIA, TOOLS, dispatch
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
        is_first = len(history_rows) <= 1
        window_left = seconds_left(wa_session)
        latest_text = _text_of(message)

        memory_block = ""
        grounded = memory_grounding.Grounded()
        if latest_text and not is_first:
            with trace.stage("grounding"):
                grounded = memory_grounding.ground(db, brand_id=brand_id, query=latest_text)
            memory_block = grounded.as_prompt_block()

        profile = lang.detect(latest_text)
        if profile.confidence >= 0.6:
            account = db.get(Account, account_id)
            if account is not None and account.locale != profile.language:
                account.locale = profile.language
                log.info(
                    "language_locked",
                    account_id=str(account_id),
                    language=profile.language,
                    script=profile.script,
                    confidence=round(profile.confidence, 2),
                )

        system = build_system(
            brand,
            memory_block,
            language_block=lang.instruction(profile),
            extra=(
                ONBOARDING_HINT if is_first
                else WINDOW_CLOSING_HINT if 0 < window_left < 3600
                else ""
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

    client = get_client()
    used_tools: list[str] = []

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
                await ctx.say(text)
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
    await ctx.say("Ek minute — kuch atak gaya. Dobara bhejein?")
    return {"ok": False, "reason": "max_turns", "tools": used_tools}
