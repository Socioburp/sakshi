"""Job handlers. One entry per job kind; the worker knows nothing else."""

from __future__ import annotations

import uuid
import uuid as _uuid

from sqlalchemy import select

from app.agent.runner import run_turn
from app.channels.base import MediaRef
from app.channels.whatsapp.adapters import get_adapter
from app.creative import logo as logo_analysis
from app.db import repo
from app.db.models import Account, Brand, BrandAsset, Message
from app.db.session import session_scope
from app.integrations.storage import r2
from app.integrations.stt import transcribe
from app.logging import get_logger
from app.memory import embed as memory_embed
from app.telemetry.stages import trace

log = get_logger(__name__)


async def handle_message(payload: dict) -> None:
    message_id = uuid.UUID(payload["message_id"])
    with trace(account_id=uuid.UUID(payload["account_id"])) as t:
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
        kind = "logo" if is_logo else "product"
        # The caption is the label the photo-first lane matches on. "coconut
        # oil 500ml" typed under a picture is worth more than any vision pass.
        with session_scope() as db:
            m = db.get(Message, message_id)
            caption = (m.text or "").strip() if m is not None else ""
        dims = _image_size(image) if not is_logo else None

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
                    label="Logo" if is_logo else (caption[:160] or None),
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
                        f"[sent a product photo, saved to their brand assets"
                        f"{': ' + caption if caption else ''}]"
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


HANDLERS = {
    "handle_message": handle_message,
    "handle_image": handle_image,
    "transcribe_and_handle": transcribe_and_handle,
    "publish_scheduled": publish_scheduled,
}
