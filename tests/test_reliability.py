"""Queue, ingest and OAuth behaviour that used to lose messages or leak access."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest


# --------------------------------------------------------------------------- #
# OAuth state
# --------------------------------------------------------------------------- #
def test_oauth_state_round_trips_and_rejects_tampering(monkeypatch):
    from app.integrations.instagram import oauth

    account_id = uuid.uuid4()
    state = oauth.sign_state(account_id)
    assert oauth.verify_state(state) == account_id

    other = uuid.uuid4()
    raw_id, ts, sig = state.split(".")
    assert oauth.verify_state(f"{other}.{ts}.{sig}") is None, "swapping the account must fail"
    assert oauth.verify_state(f"{raw_id}.{ts}.{'0' * 32}") is None
    assert oauth.verify_state(str(account_id)) is None, "the old bare-uuid state is refused"

    real_now = time.time()
    monkeypatch.setattr(oauth.time, "time", lambda: real_now + oauth.STATE_TTL_S + 1)
    assert oauth.verify_state(state) is None, "a link older than the TTL is refused"


# --------------------------------------------------------------------------- #
# worker
# --------------------------------------------------------------------------- #
def test_retry_backoff_grows_and_is_capped():
    from app.queue import client

    pushed = []
    client_push = client._push
    try:
        client._push = lambda envelope, at=None: pushed.append(at) or True
        now = datetime.now(UTC)
        for attempt in (1, 2, 9):
            client.retry_later("j", "handle_message", {}, attempt)
        delays = [(at - now).total_seconds() for at in pushed]
        assert 9 <= delays[0] <= 11 and 59 <= delays[1] <= 61
        assert 59 <= delays[2] <= 61, "attempts past the table use the last backoff"
    finally:
        client._push = client_push


# --------------------------------------------------------------------------- #
# webhook URL behind a proxy
# --------------------------------------------------------------------------- #
def test_twilio_signing_url_is_the_public_one(monkeypatch):
    from app.channels.whatsapp import router

    req = SimpleNamespace(
        url=SimpleNamespace(
            path="/webhooks/whatsapp",
            query="a=1",
            __str__=lambda s: "http://10.0.0.5/webhooks/whatsapp?a=1",
        )
    )
    monkeypatch.setattr(router.settings, "public_base_url", "https://sakshi.example.com/")
    assert router._public_url(req) == "https://sakshi.example.com/webhooks/whatsapp?a=1"


# --------------------------------------------------------------------------- #
# history
# --------------------------------------------------------------------------- #
def test_a_turn_covers_every_unanswered_inbound_since_the_last_reply():
    from app.agent.runner import _covered, _history

    def m(direction, text, answered=None):
        return SimpleNamespace(
            id=uuid.uuid4(),
            direction=direction,
            kind="text",
            text=text,
            transcript=None,
            answered_at=answered,
        )

    a = m("in", "hi", answered=datetime.now(UTC))
    r1 = m("out", "Namaste!")
    b, c = m("in", "sale post banao"), m("in", "20% off likhna")
    assert _covered([a, r1, b, c], c) == [b.id, c.id], "b and c fold into one user turn"
    # A message that arrived mid-turn is NOT covered by a reply recorded after it:
    # r2 answered b only, and c is still unanswered when its own job runs.
    r2 = m("out", "Theek hai")
    assert _covered([a, r1, b, r2, c], c) == [c.id]
    # Belt and braces: history handed to the model never ends on our turn.
    assert _history([a, r1, b, r2])[-1]["role"] == "user"


# --------------------------------------------------------------------------- #
# onboarding
# --------------------------------------------------------------------------- #
def test_no_logo_is_an_answer_not_a_gap():
    from app.agent.prompts import missing_setup

    brand = SimpleNamespace(category="sweets", logo_url=None, template_prefs={})
    assert missing_setup(brand) == ["logo"]
    brand.template_prefs = {"no_logo": True}
    assert missing_setup(brand) == []


# --------------------------------------------------------------------------- #
# memory
# --------------------------------------------------------------------------- #
def test_prod_refuses_to_write_unretrievable_memory(monkeypatch):
    from app.memory import embed

    monkeypatch.setattr(embed.settings, "voyage_api_key", "")
    monkeypatch.setattr(embed.settings, "env", "prod")
    with pytest.raises(RuntimeError):
        embed.embed_texts(["anything"])
    monkeypatch.setattr(embed.settings, "env", "dev")
    assert embed.embed_texts(["anything"])[0] == [0.0] * embed.settings.embed_dim


# --------------------------------------------------------------------------- #
# instagram auth errors
# --------------------------------------------------------------------------- #
def test_dead_token_is_recognised():
    import httpx

    from app.integrations.instagram import client as ig

    req = httpx.Request("POST", "https://graph.instagram.com/x")
    dead = httpx.HTTPStatusError(
        "400",
        request=req,
        response=httpx.Response(
            400, json={"error": {"code": 190, "type": "OAuthException"}}, request=req
        ),
    )
    # Meta labels almost every Graph error "OAuthException"; only the code counts.
    other = httpx.HTTPStatusError(
        "400",
        request=req,
        response=httpx.Response(
            400, json={"error": {"code": 100, "type": "OAuthException"}}, request=req
        ),
    )
    assert ig.is_auth_error(dead) and not ig.is_auth_error(other)
    assert not ig.is_auth_error(RuntimeError("x"))
