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

    # How many separate chat messages one turn may send before the rest are
    # folded into the closing reply. WhatsApp is not a log: three notifications
    # for one request reads as a machine talking to itself, and on a phone each
    # one is a separate buzz. Two is a status line plus an answer.
    say_budget: int = 2
    _said: int = 0
    _deferred: list[str] = field(default_factory=list)

    async def say(
        self, text: str, buttons: list[Button] | None = None, final: bool = False
    ) -> bool:
        """Send a chat message, or hold it back to travel with the closing one.

        True if the provider accepted the message. Callers must not assume.

        Nothing is ever dropped: over-budget lines are kept and prepended to
        the turn's final reply, which the runner marks with `final=True`. That
        is also why the budget is safe to tighten -- the worst case is one
        longer message, never a lost one.
        """
        body = (text or "").strip()
        if final:
            if self._deferred:
                body = "\n\n".join([*self._deferred, body]).strip()
                self._deferred.clear()
        elif self._said >= self.say_budget:
            if body:
                self._deferred.append(body)
            return True
        if not body:
            return True
        self._said += 1
        return await send.send_text(
            account_id=self.account_id,
            session_id=self.session_id,
            wa_id=self.wa_id,
            text=body,
            buttons=buttons,
        )

    async def show_video(self, video_url: str, caption: str = "") -> bool:
        ok = await send.send_video(
            account_id=self.account_id,
            session_id=self.session_id,
            wa_id=self.wa_id,
            video_url=video_url,
            caption=caption,
        )
        if ok:
            self.sent_media.append(video_url)
        return ok

    async def show(self, image_url: str, caption: str = "") -> bool:
        ok = await send.send_image(
            account_id=self.account_id,
            session_id=self.session_id,
            wa_id=self.wa_id,
            image_url=image_url,
            caption=caption,
        )
        if ok:
            self.sent_media.append(image_url)
        return ok
