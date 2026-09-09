from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.channels.base import Button
from app.channels.whatsapp import send
from app.memory.grounding import Grounded
from app.telemetry.stages import Trace


@dataclass
class ToolContext:
    """Everything a tool is allowed to touch. Passed to every dispatch call."""

    account_id: uuid.UUID
    brand_id: uuid.UUID
    session_id: uuid.UUID | None
    wa_id: str
    trace: Trace
    message_id: uuid.UUID | None = None
    sent_media: list[str] = field(default_factory=list)
    # What retrieval actually returned this turn. Stamped onto the brief
    # server-side rather than trusted from the model -- a self-reported
    # grounding list tells you what the model believed, not what it was given,
    # which is useless precisely when you are debugging a bad creative.
    grounding: Grounded = field(default_factory=Grounded)

    async def say(self, text: str, buttons: list[Button] | None = None) -> None:
        await send.send_text(
            account_id=self.account_id,
            session_id=self.session_id,
            wa_id=self.wa_id,
            text=text,
            buttons=buttons,
        )

    async def show(self, image_url: str, caption: str = "") -> None:
        await send.send_image(
            account_id=self.account_id,
            session_id=self.session_id,
            wa_id=self.wa_id,
            image_url=image_url,
            caption=caption,
        )
        self.sent_media.append(image_url)
