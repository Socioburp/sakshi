"""Instagram webhook: verify it is really Meta, and read what happened.

Two shapes arrive on the same endpoint. A comment is a `changes` entry with
`field: "comments"`; a DM is a `messaging` entry. Both are flattened into one
`IgInbound` the inbox can persist without caring which it was.

Verification mirrors the WhatsApp webhook: the GET handshake echoes
`hub.challenge`, and every POST carries `X-Hub-Signature-256: sha256=<hmac>`
over the raw body, keyed with the app secret. An unsigned or wrongly-signed
body is rejected before it can create a row.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.logging import get_logger

log = get_logger(__name__)


def _secret() -> bytes:
    key = settings.ig_app_secret or settings.wa_app_secret
    if not key:
        if settings.is_prod:
            raise RuntimeError("IG_APP_SECRET is unset; cannot verify webhook signatures")
        key = "dev-only-state-secret"
    return key.encode()


def verify_challenge(params: dict[str, str]) -> str | None:
    """The subscription handshake. Returns the challenge to echo, or None."""
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == (
        settings.ig_verify_token or settings.wa_verify_token
    ):
        return params.get("hub.challenge")
    return None


def verify_signature(raw: bytes, headers: dict[str, str]) -> bool:
    """HMAC-SHA256 of the raw body against X-Hub-Signature-256.

    In dev, with no app secret configured and no header sent, we accept: the
    mock has no secret to sign with. In prod a missing or bad signature is a
    hard reject (`_secret` raises if the secret itself is unset in prod).
    """
    header = headers.get("x-hub-signature-256") or headers.get("x-hub-signature-256".title())
    if not header:
        return not settings.is_prod
    try:
        algo, sent = header.split("=", 1)
    except ValueError:
        return False
    if algo != "sha256":
        return False
    expected = hmac.new(_secret(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sent)


@dataclass(slots=True)
class IgInbound:
    kind: str  # comment | mention | message
    ig_object_id: str  # comment id or message id (the reply target / dedupe key)
    recipient_ig_id: str  # the account that owns the post/thread (routes to a brand)
    from_id: str | None = None
    from_username: str | None = None
    text: str | None = None
    media_id: str | None = None
    parent_id: str | None = None
    is_echo: bool = False  # a message the account itself sent (message_echoes)
    raw: dict[str, Any] = field(default_factory=dict)


def parse(body: dict[str, Any]) -> list[IgInbound]:
    """Flatten a webhook body into inbound comments and messages.

    Never raises on a shape it does not know: an unexpected field is skipped,
    not fatal, because the endpoint must always 200 or Meta retries forever.
    """
    out: list[IgInbound] = []
    if not isinstance(body, dict) or body.get("object") not in ("instagram", None):
        # `object` is "instagram" for this product; tolerate its absence in tests.
        if body.get("object") not in (None, "instagram"):
            return out
    for entry in body.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        account_id = str(entry.get("id") or "")
        for change in entry.get("changes") or []:
            item = _from_change(change, account_id)
            if item is not None:
                out.append(item)
        for m in entry.get("messaging") or []:
            item = _from_messaging(m, account_id)
            if item is not None:
                out.append(item)
    return out


def _from_change(change: dict[str, Any], account_id: str) -> IgInbound | None:
    if not isinstance(change, dict):
        return None
    field_name = change.get("field")
    value = change.get("value") or {}
    if field_name not in ("comments", "mentions") or not isinstance(value, dict):
        return None
    cid = str(value.get("id") or "")
    if not cid:
        return None
    frm = value.get("from") or {}
    media = value.get("media") or {}
    return IgInbound(
        kind="mention" if field_name == "mentions" else "comment",
        ig_object_id=cid,
        recipient_ig_id=account_id,
        from_id=str(frm.get("id")) if frm.get("id") else None,
        from_username=frm.get("username"),
        text=value.get("text"),
        media_id=str(media.get("id")) if media.get("id") else None,
        parent_id=str(value.get("parent_id")) if value.get("parent_id") else None,
        raw=change,
    )


def _from_messaging(m: dict[str, Any], account_id: str) -> IgInbound | None:
    if not isinstance(m, dict):
        return None
    message = m.get("message") or {}
    mid = str(message.get("mid") or "")
    if not mid:
        return None  # reactions/seen/postbacks carry no mid; ignored for now
    sender = str((m.get("sender") or {}).get("id") or "")
    recipient = str((m.get("recipient") or {}).get("id") or "") or account_id
    is_echo = bool(message.get("is_echo"))
    return IgInbound(
        kind="message",
        ig_object_id=mid,
        recipient_ig_id=recipient,
        from_id=sender or None,
        text=message.get("text"),
        is_echo=is_echo,
        raw=m,
    )


__all__ = ["IgInbound", "parse", "verify_challenge", "verify_signature"]
