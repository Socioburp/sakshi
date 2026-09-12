"""Job handlers. One entry per job kind; the worker knows nothing else."""

from __future__ import annotations

import asyncio
import uuid
import uuid as _uuid

from sqlalchemy import func, select

from app.agent.runner import run_turn
from app.channels.base import MediaRef
from app.channels.whatsapp.adapters import get_adapter
from app.config import settings
from app.creative import logo as logo_analysis
from app.creative import photo_quality
from app.db import repo
from app.db.models import Account, Brand, BrandAsset, Message, WaSession
from app.db.session import session_scope
from app.integrations.storage import r2
from app.integrations.stt import transcribe
from app.logging import get_logger
from app.memory import embed as memory_embed
from app.telemetry.stages import trace

log = get_logger(__name__)


async def handle_message(payload: dict) -> None:
    message_id = uuid.UUID(payload["message_id"])
    account_id = uuid.UUID(payload["account_id"])
    if payload.get("interactive_id"):
        # The memory half of a tap: a network call to the embedding vendor,
        # kept off the webhook and out of its transaction.
        from app.insights import votes

        with session_scope() as db:
            votes.remember_tap(db, payload["interactive_id"], account_id)
    with trace(account_id=account_id) as t:
        await run_turn(message_id=message_id, trace=t)


async def transcribe_and_handle(payload: dict) -> None:
    """The riskiest path in the product: voice note in, brief out."""
    message_id = uuid.UUID(payload["message_id"])
    account_id = uuid.UUID(payload["account_id"])

    with trace(account_id=account_id) as t:
        adapter = get_adapter(payload.get("provider"))
        ref = MediaRef(
            id=payload.get("media_id"),
            url=payload.get("media_url"),
            mime=payload.get("media_mime"),
        )
        with session_scope() as db:
            existing = db.get(Message, message_id)
            if existing is not None and existing.transcript:
                # A re-run (reaper, duplicate delivery): the transcript is
                # already there; do not pay the vendor twice.
                await run_turn(message_id=message_id, trace=t)
                return

        try:
            with t.stage("media_download"):
                audio, mime = await adapter.download_media(ref)
        except Exception:  # noqa: BLE001
            log.exception("media_download_failed", message_id=str(message_id))
            await run_turn(message_id=message_id, trace=t)
            return

        with session_scope() as db:
            account = db.get(Account, account_id)
            # The locale we locked from their typed messages is a far better STT
            # hint than a default -- a Kannada speaker's voice note transcribed
            # as Hindi loses exactly the words that matter, the product names.
            # "en-IN" is the column default, never a locked choice (English is
            # not lockable from text), so it is not evidence about this voice
            # note. Forcing a Kannada speaker's first note through English
            # loses exactly the product names. No hint = provider auto-detect.
            # brand.languages is seeded ["en", "hi"] for everyone, so it would
            # force a Kannada speaker's first note through Hindi. The only hint
            # worth giving is a locale the owner has actually locked.
            locale = account.locale if account else None
            hints = [locale] if locale and not locale.lower().startswith("en") else []

        result = None
        try:
            with t.stage("stt", provider=None, bytes=len(audio)):
                result = await transcribe(audio, mime, hint_languages=hints or None)
        except Exception:  # noqa: BLE001
            # The riskiest vendor call in the product must not end in silence.
            # The runner sees "[voice note, could not be transcribed]" and the
            # agent asks them to type it -- one line, in their language.
            log.exception("stt_failed", message_id=str(message_id), hints=hints)

        with session_scope() as db:
            msg = db.get(Message, message_id)
            msg.media_mime = mime
            if result is not None:
                msg.transcript = result.text
                msg.transcript_provider = result.provider
                msg.transcript_lang = result.language
                msg.transcript_confidence = result.confidence

        if result is not None:
            log.info(
                "voice_note_transcribed",
                message_id=str(message_id),
                provider=result.provider,
                lang=result.language,
                chars=len(result.text),
            )
        await run_turn(message_id=message_id, trace=t)


async def handle_image(payload: dict) -> None:
    """An inbound picture. The first one is almost always the logo.

    The palette is measured from the pixels rather than asked for, because a
    client who says "our colour is dark green" and a logo that is #175B3D are
    two different facts, and the second one is the one the creatives have to
    match.
    """
    message_id = _uuid.UUID(payload["message_id"])
    account_id = _uuid.UUID(payload["account_id"])

    with trace(account_id=account_id) as t:
        with session_scope() as db:
            already = db.scalar(
                select(BrandAsset).where(BrandAsset.source_message_id == message_id).limit(1)
            )
        if already is not None:
            # A re-run: the photo is stored and the brand updated; just answer.
            await run_turn(message_id=message_id, trace=t)
            return

        adapter = get_adapter(payload.get("provider"))
        ref = MediaRef(
            id=payload.get("media_id"),
            url=payload.get("media_url"),
            mime=payload.get("media_mime"),
        )
        with t.stage("media_download"):
            image, mime = await adapter.download_media(ref)

        with session_scope() as db:
            brand = repo.default_brand(db, account_id)
            brand_id = brand.id if brand else None
            brand_name = brand.name if brand else "your brand"
            already_has_logo = bool(
                brand and (brand.logo_url or (brand.template_prefs or {}).get("no_logo"))
            )

        if brand_id is None:
            await run_turn(message_id=message_id, trace=t)
            return

        # Decided NOW, not when the webhook landed. An owner who sends the logo
        # and a product shot in the same breath produced two payloads both
        # flagged as the logo candidate; the second one used to overwrite the
        # first -- logo, palette and notes replaced by a photo of a jar.
        is_logo = bool(payload.get("is_logo_candidate", True)) and not already_has_logo
        # The caption is the label the photo-first lane matches on. "coconut
        # oil 500ml" typed under a picture is worth more than any vision pass.
        with session_scope() as db:
            m = db.get(Message, message_id)
            caption = (m.text or "").strip() if m is not None else ""
        dims = _image_size(image) if not is_logo else None
        kind, label = "logo", "Logo"
        quality_note = ""
        if not is_logo:
            # Measured before anything is built on it: a blurry or dark photo
            # is named to the owner now, while a retake costs them nothing.
            quality = await asyncio.to_thread(photo_quality.assess, image)
            log.info("photo_quality", message_id=str(message_id), **quality.as_dict())
            quality_note = quality.owner_note()
            # What IS this photo? The product lane cuts out and re-stages
            # anything stored as "product"; a shopfront or the owner's face
            # must never get that treatment, so the kind is decided by vision
            # when a model is configured, and conservatively when it is not.
            with t.stage("photo_classify"):
                try:
                    seen = await logo_analysis.describe_photo(image, mime or "image/jpeg")
                except Exception:  # noqa: BLE001
                    log.warning("photo_vision_failed", message_id=str(message_id))
                    seen = {}
            if seen.get("kind"):
                kind = seen["kind"]
                if kind == "product" and seen.get("cut_out_ok") is False:
                    kind = "other"  # a plate, a set, glass: keep the photo whole
            else:
                kind = "product" if caption else "other"
            label = (caption or seen.get("label") or "")[:160] or None

        with t.stage("asset_upload"):
            ext = "png" if "png" in (mime or "") else "jpg"
            key = r2.key_for(str(brand_id), str(message_id), f"{kind}.{ext}", draft=False)
            url = r2.put(key, image, mime or "image/png")

        analysis = None
        if is_logo:
            with t.stage("logo_analysis"):
                analysis = await logo_analysis.analyse(image, mime or "image/png")

        with session_scope() as db:
            db.add(
                BrandAsset(
                    brand_id=brand_id,
                    kind=kind,
                    label=label,
                    storage_key=key,
                    url=url,
                    mime=mime,
                    width=analysis.width if analysis else (dims[0] if dims else None),
                    height=analysis.height if analysis else (dims[1] if dims else None),
                    source_message_id=message_id,
                )
            )
            if analysis is not None:
                brand = db.get(Brand, brand_id)
                brand.logo_url = url
                # Never overwrite colours the client stated themselves.
                brand.palette = {**analysis.palette, **(brand.palette or {})}
                brand.logo_notes = analysis.summary
                brand.logo_analysis = {
                    "palette": analysis.palette,
                    "swatches": analysis.swatches,
                    "has_transparency": analysis.has_transparency,
                    "style": analysis.style,
                    "tone": analysis.tone,
                    "has_wordmark": analysis.has_wordmark,
                }
                try:
                    memory_embed.remember(
                        db,
                        brand_id=brand_id,
                        kind="fact",
                        content=analysis.as_prose(brand_name),
                        source_ref=f"message:{message_id}",
                    )
                except Exception:  # noqa: BLE001
                    log.warning("logo_memory_write_failed", brand_id=str(brand_id))

            msg = db.get(Message, message_id)
            if msg is not None:
                msg.text = (
                    f"[sent their logo] measured colours: "
                    f"{', '.join(f'{k} {v}' for k, v in analysis.palette.items())}"
                    + (f"; {analysis.summary}" if analysis.summary else "")
                    if analysis
                    else (
                        f"[sent a photo ({kind}), saved to their brand assets"
                        f"{': ' + label if label else ''}"
                        f"{'; QUALITY: ' + quality_note if quality_note else ''}]"
                    )
                )

        log.info(
            "image_ingested",
            message_id=str(message_id),
            kind=kind,
            palette=(analysis.palette if analysis else None),
        )
        await run_turn(message_id=message_id, trace=t)


def _image_size(data: bytes) -> tuple[int, int] | None:
    """Pixel size of an inbound photo. Only the header is decoded."""
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(data)) as im:
            return im.size
    except Exception:  # noqa: BLE001
        return None


async def publish_scheduled(payload: dict) -> None:
    from app.agent.context import ToolContext
    from app.agent.tools import _publish_to_instagram

    with trace(account_id=uuid.UUID(payload["account_id"])) as t:
        ctx = ToolContext(
            account_id=uuid.UUID(payload["account_id"]),
            brand_id=uuid.UUID(payload["brand_id"]),
            session_id=None,
            wa_id=payload.get("wa_id", ""),
            trace=t,
        )
        await _publish_to_instagram(ctx, {"creative_id": payload["creative_id"]})


async def ig_event_notify(payload: dict) -> None:
    """A comment or DM arrived: draft a reply and send it to the owner to approve.

    The draft (an LLM call) and the WhatsApp send both happen here, off the
    webhook. Re-running is safe: an event already past 'new' is left alone, so
    a duplicate job never nudges the owner twice.
    """
    from app.agent import buttons as btn
    from app.channels.whatsapp import send
    from app.db.models import IgEvent
    from app.inbox import service

    event_id = uuid.UUID(payload["event_id"])
    with session_scope() as db:
        ev = db.get(IgEvent, event_id)
        if ev is None or ev.status != "new":
            return
        brand = db.get(Brand, ev.brand_id)
        if brand is None:
            return
        acct = db.get(Account, brand.account_id)
        wa_id = acct.wa_phone if acct else None
        locale = (acct.locale if acct else None) or "en"
        # Only reachable inside the 24h window; outside it a template message
        # would be needed, which is a later feature. Leave the event 'new' so a
        # future inbound could resurface it.
        sess = repo.latest_session(db, wa_id, account_id=brand.account_id) if wa_id else None
        if sess is None or not repo.window_is_open(sess):
            log.info("ig_event_window_closed", event_id=str(event_id))
            return
        session_id = sess.id
        kind, text = ev.kind, ev.text or ""
        from_username = ev.from_username
        facts = service.facts_for(db, brand.id, text)
        brand_obj = brand

    lang = locale.split("-")[0].lower()
    reply = await service.draft(kind=kind, text=text, brand=brand_obj, facts=facts, lang=lang)
    with session_scope() as db:
        ev = db.get(IgEvent, event_id)
        if ev is None or ev.status != "new":
            return  # a tap raced us; do not overwrite
        ev.draft_reply = reply
        ev.status = "drafted"
    message = service.owner_message(
        kind=kind, from_username=from_username, text=text, draft_text=reply, lang=lang
    )
    ok = await send.send_text(
        account_id=brand_obj.account_id,
        session_id=session_id,
        wa_id=wa_id,
        text=message[:1000],
        buttons=btn.ig_buttons(str(event_id), locale),
    )
    log.info("ig_event_notified" if ok else "ig_event_notify_suppressed", event_id=str(event_id))


async def ig_action(payload: dict) -> None:
    """Act on the owner's tap: send the drafted reply, post an edit, or skip.

    Posting to Instagram (the reply or DM) is the network call kept off the
    webhook. Every path ends in one confirmation line to the owner.
    """
    from app.channels.whatsapp import send
    from app.db.models import IgAccount, IgEvent
    from app.integrations.instagram import client as ig

    event_id = uuid.UUID(payload["event_id"])
    action = payload["action"]  # send | skip | edit_prompt | custom
    custom_text = (payload.get("custom_text") or "").strip()

    with session_scope() as db:
        ev = db.get(IgEvent, event_id)
        if ev is None:
            return
        brand = db.get(Brand, ev.brand_id)
        acct = db.get(Account, brand.account_id) if brand else None
        wa_id = acct.wa_phone if acct else None
        locale = (acct.locale if acct else None) or "en"
        sess = repo.latest_session(db, wa_id, account_id=brand.account_id) if wa_id else None
        session_id = sess.id if sess else None
        status_now = ev.status
        kind = ev.kind
        object_id = ev.ig_object_id
        from_id = ev.from_id
        draft_reply = ev.draft_reply or ""
        ig_row = db.get(IgAccount, ev.ig_account_id) if ev.ig_account_id else None
        token = ig_row.access_token if ig_row else None
        ig_user_id = ig_row.ig_user_id if ig_row else None
    lang = locale.split("-")[0].lower()

    async def _confirm(text: str) -> None:
        if wa_id and session_id and brand is not None:
            await send.send_text(
                account_id=brand.account_id, session_id=session_id, wa_id=wa_id, text=text
            )

    if action == "edit_prompt":
        await _confirm(_IG_EDIT_PROMPT.get(lang, _IG_EDIT_PROMPT["en"]))
        return
    if action == "skip":
        if status_now in ("new", "drafted", "approved"):
            with session_scope() as db:
                ev = db.get(IgEvent, event_id)
                if ev and ev.status in ("new", "drafted", "approved"):
                    ev.status = "skipped"
        await _confirm(_IG_SKIPPED.get(lang, _IG_SKIPPED["en"]))
        return
    if status_now in ("sent",):
        return  # already posted; a double tap is a no-op

    reply_text = custom_text if action == "custom" else draft_reply
    if not reply_text:
        await _confirm(_IG_SKIPPED.get(lang, _IG_SKIPPED["en"]))
        return
    if ig_row is None or not token:
        with session_scope() as db:
            ev = db.get(IgEvent, event_id)
            if ev:
                ev.status, ev.error = "failed", "instagram_not_connected"
        await _confirm(_IG_RECONNECT.get(lang, _IG_RECONNECT["en"]))
        return
    try:
        if kind in ("comment", "mention"):
            reply_id = await ig.reply_to_comment(
                comment_id=object_id, access_token=token, message=reply_text
            )
        else:
            reply_id = await ig.send_dm(
                ig_user_id=ig_user_id, access_token=token, recipient_id=from_id, text=reply_text
            )
    except Exception as exc:  # noqa: BLE001
        log.exception("ig_reply_failed", event_id=str(event_id))
        with session_scope() as db:
            ev = db.get(IgEvent, event_id)
            if ev:
                ev.status, ev.error = "failed", str(exc)[:2000]
                if ig.is_auth_error(exc) and ev.ig_account_id:
                    row = db.get(IgAccount, ev.ig_account_id)
                    if row:
                        row.status = "disconnected"
        msg = _IG_RECONNECT if ig.is_auth_error(exc) else _IG_FAILED
        await _confirm(msg.get(lang, msg["en"]))
        return
    with session_scope() as db:
        ev = db.get(IgEvent, event_id)
        if ev:
            ev.status, ev.reply_text, ev.reply_ig_id = "sent", reply_text, str(reply_id)
    await _confirm(_IG_SENT.get(lang, _IG_SENT["en"]))
    log.info("ig_reply_sent", event_id=str(event_id), kind=kind)


_IG_EDIT_PROMPT = {
    "en": "Type the reply you'd like to post, and I'll send it. Or tap Skip on the message above.",
    "hi": "Jo reply post karna hai wo type karein, main bhej dunga. Ya upar Skip dabayein.",
}
_IG_SENT = {"en": "Posted ✅", "hi": "Post ho gaya ✅"}
_IG_SKIPPED = {"en": "Skipped — nothing was posted.", "hi": "Chhod diya — kuch post nahi hua."}
_IG_FAILED = {
    "en": "Couldn't post that reply just now. Try again in a bit.",
    "hi": "Abhi reply post nahi ho paya. Thodi der mein phir se try karein.",
}
_IG_RECONNECT = {
    "en": "Instagram needs reconnecting before I can reply. Ask me to connect it.",
    "hi": "Reply se pehle Instagram dobara connect karna hoga. Mujhse connect karne ko kahein.",
}


async def sync_insights(payload: dict) -> None:
    """Read a brand's Instagram numbers. Queued hourly by the sweep (once a
    day per brand) and 48h after each publish; a no-op without a connection."""
    from app.insights import performance

    result = await performance.sync_brand(uuid.UUID(payload["brand_id"]))
    if not result.get("ok") and result.get("reason") == "sync_failed":
        # Let the worker's retry/backoff have a go: a vendor blip, not a fact.
        raise RuntimeError(result.get("error") or "insights sync failed")


async def daily_suggestion(payload: dict) -> None:
    """Tomorrow's post, offered before they ask. One line, three buttons.

    Sent only inside the 24h window (send.py refuses otherwise), only if the
    owner has not switched it off, and only if nothing was suggested in the
    last 20 hours. A nudge that arrives twice is spam; one that arrives when
    they cannot reply is a template message, which is a later feature.
    """
    from app.agent import buttons as btn
    from app.channels.whatsapp import send
    from app.insights import suggest as sg
    from app.insights import votes

    account_id = uuid.UUID(payload["account_id"])
    brand_id = uuid.UUID(payload["brand_id"])
    wa_id = payload.get("wa_id", "")
    if not wa_id:
        return
    with session_scope() as db:
        brand = db.get(Brand, brand_id)
        if brand is None or (brand.template_prefs or {}).get("daily_nudge") is False:
            return
        if votes.nudge_snoozed(brand):
            log.info("daily_suggestion_snoozed", brand_id=str(brand_id))
            return
        if sg.suggested_recently(db, brand_id):
            return
        sess = repo.latest_session(db, wa_id, account_id=account_id)
        if sess is None or not repo.window_is_open(sess):
            # Outside the 24h window only a template message reaches them,
            # and that is a later feature. Nothing is recorded: the next
            # inbound message re-arms the schedule.
            log.info("daily_suggestion_window_closed", brand_id=str(brand_id))
            return
        # Photos are the ceiling on every post. Before the first idea, a brand
        # with fewer than three photos on file gets the photo-day checklist
        # once, instead of an idea it cannot yet build well.
        photo_count = db.scalar(
            select(func.count(BrandAsset.id)).where(
                BrandAsset.brand_id == brand_id, BrandAsset.kind != "logo"
            )
        )
        checklist_text = None
        if (photo_count or 0) < 3 and not (brand.template_prefs or {}).get("shotlist_sent"):
            from app.creative import shotlist

            acct = db.get(Account, account_id)
            lang = ((acct.locale if acct else None) or "en").split("-")[0].lower()
            checklist_text = shotlist.checklist(brand.category, "hi" if lang == "hi" else "en")
            brand.template_prefs = {**(brand.template_prefs or {}), "shotlist_sent": True}
            session_id = sess.id
    if checklist_text:
        # Sent outside the transaction, like every other network call.
        ok = await send.send_text(
            account_id=account_id, session_id=session_id, wa_id=wa_id, text=checklist_text
        )
        log.info(
            "photo_checklist_sent" if ok else "photo_checklist_suppressed",
            brand_id=str(brand_id),
        )
        return
    # Fresh numbers before the idea is chosen: the sweep usually got here
    # first, in which case this is a no-op. A failed read never costs the nudge.
    try:
        from app.insights import performance

        await performance.sync_if_due(brand_id)
    except Exception:  # noqa: BLE001
        log.exception("daily_suggestion_sync_failed", brand_id=str(brand_id))
    with session_scope() as db:
        brand = db.get(Brand, brand_id)
        sess = repo.latest_session(db, wa_id, account_id=account_id)
        ideas = sg.suggest(db, brand)
        if not ideas:
            return
        session_id = sess.id
        best = ideas[0]
        acct = db.get(Account, account_id)
        locale = (acct.locale if acct else None) or "en"
        idea_dicts = [i.as_dict() for i in ideas]
        # Monday: last week's numbers and the week's line-up ride above the
        # idea, once per week.
        week_lines = _week_lineup(db, brand, locale)

    lang = locale.split("-")[0].lower()
    line = await _phrase_nudge(best, lang)
    if week_lines:
        line = f"{week_lines}\n\n{line}"
    ok = await send.send_text(
        account_id=account_id,
        session_id=session_id,
        wa_id=wa_id,
        text=line[:1000],
        buttons=btn.buttons(["make:1", "next", "skip"], locale),
    )
    if ok:
        # Only a nudge that reached the phone counts as "suggested" -- and only
        # then does the tap next turn have ideas to point at.
        with session_scope() as db:
            sess = db.get(WaSession, session_id)
            if sess is not None:
                sess.state = {**(sess.state or {}), "suggestions": idea_dicts}
            brand = db.get(Brand, brand_id)
            if brand is not None:
                sg.record_suggested(db, brand, ideas)
    log.info(
        "daily_suggestion_sent" if ok else "daily_suggestion_suppressed", brand_id=str(brand_id)
    )


def _week_lineup(db, brand: Brand, locale: str) -> str | None:
    """Monday, once: last week's Instagram numbers, then the plan's line-up."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.insights import performance
    from app.insights import plan as planning

    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    if today.weekday() != 0:
        return None
    stamp = today.isoformat()
    prefs = brand.template_prefs or {}
    if prefs.get("plan_week_sent") == stamp:
        return None
    lang = (locale or "en").split("-")[0].lower()
    parts = []
    try:
        readout = performance.week_readout(db, brand.id, today, lang)
    except Exception:  # noqa: BLE001 - the line-up must not wait on the numbers
        log.exception("week_readout_failed", brand_id=str(brand.id))
        readout = None
    if readout:
        parts.append(readout)
    lineup = planning.week_message(planning.current(db, brand.id, today), today, lang)
    if lineup:
        parts.append(lineup)
    if parts:
        brand.template_prefs = {**prefs, "plan_week_sent": stamp}
    return "\n\n".join(parts) or None


async def _phrase_nudge(idea, lang: str) -> str:
    """One warm line in the owner's language.

    The model rewrites the idea and its reason as a friend would say it; when
    no model is configured, or it fails, the plain frame below is sent. The
    output is clipped to two short lines -- a nudge is not a pitch.
    """
    fallback = _NUDGE.get(lang, _NUDGE["en"]).format(idea=idea.headline_idea, why=idea.why)
    if not settings.anthropic_api_key or not settings.anthropic_model:
        return fallback
    try:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        resp = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=120,
            system=(
                "You write ONE short WhatsApp line (max 25 words) from a marketing designer "
                "to a small shop owner in India, in the language of this code: "
                f"{lang}. Latin script for hi/mr/kn/ta/te/ml (Hinglish-style). Offer the post "
                "idea and its reason, end by asking if you should make it. No emoji, no lists, "
                "no greeting, no quotes."
            ),
            messages=[
                {
                    "role": "user",
                    "content": f"Idea: {idea.headline_idea}\nReason: {idea.why}",
                }
            ],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        text = " ".join(text.split())
        return text if 10 <= len(text) <= 240 else fallback
    except Exception:  # noqa: BLE001 - the plain frame is a fine nudge
        log.warning("nudge_phrasing_failed", lang=lang)
        return fallback


_NUDGE = {
    "hi": "Aaj ke liye ek idea: {idea} — {why}. Banau?",
    "en": "An idea for today: {idea} — {why}. Shall I make it?",
    "kn": "Ivattu ondu idea: {idea} — {why}. Maadla?",
    "ta": "Innaikku oru idea: {idea} — {why}. Pannattuma?",
    "te": "Ee roju oka idea: {idea} — {why}. Cheyyamanta?",
    "mr": "Aaj sathi ek idea: {idea} — {why}. Banavu ka?",
    "ml": "Innu oru idea: {idea} — {why}. Cheyyatte?",
}


HANDLERS = {
    "handle_message": handle_message,
    "daily_suggestion": daily_suggestion,
    "handle_image": handle_image,
    "transcribe_and_handle": transcribe_and_handle,
    "publish_scheduled": publish_scheduled,
    "sync_insights": sync_insights,
    "ig_event_notify": ig_event_notify,
    "ig_action": ig_action,
}
