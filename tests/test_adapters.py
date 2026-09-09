import hashlib
import hmac
import json

import pytest

from app.channels.whatsapp.adapters.meta import MetaAdapter
from app.channels.whatsapp.adapters.mock import MockAdapter
from app.channels.whatsapp.adapters.twilio import TwilioAdapter
from app.config import settings

META_TEXT = {
    "entry": [
        {
            "changes": [
                {
                    "value": {
                        "contacts": [{"wa_id": "919876543210", "profile": {"name": "Anil"}}],
                        "messages": [
                            {
                                "from": "919876543210",
                                "id": "wamid.ABC",
                                "timestamp": "1757280000",
                                "type": "text",
                                "text": {"body": "kal sale hai"},
                            }
                        ],
                    }
                }
            ]
        }
    ]
}

META_AUDIO = json.loads(json.dumps(META_TEXT))
META_AUDIO["entry"][0]["changes"][0]["value"]["messages"][0] = {
    "from": "919876543210",
    "id": "wamid.AUD",
    "timestamp": "1757280001",
    "type": "audio",
    "audio": {"id": "media-123", "mime_type": "audio/ogg; codecs=opus", "voice": True},
}


def test_meta_parses_text():
    (m,) = MetaAdapter().parse(META_TEXT, {})
    assert (m.kind, m.text, m.wa_id, m.profile_name) == (
        "text", "kal sale hai", "919876543210", "Anil",
    )


def test_meta_parses_audio_separately_from_text():
    (m,) = MetaAdapter().parse(META_AUDIO, {})
    assert m.kind == "audio"
    assert m.text is None
    assert m.media.id == "media-123"


def test_meta_verify_handshake():
    a = MetaAdapter()
    assert a.verify_webhook(
        {"hub.mode": "subscribe", "hub.verify_token": settings.wa_verify_token,
         "hub.challenge": "12345"}
    ) == "12345"
    assert a.verify_webhook({"hub.mode": "subscribe", "hub.verify_token": "wrong"}) is None


def test_meta_signature(monkeypatch):
    monkeypatch.setattr(settings, "wa_app_secret", "s3cret")
    body = b'{"a":1}'
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    a = MetaAdapter()
    assert a.verify_signature(body, {"x-hub-signature-256": sig})
    assert not a.verify_signature(body, {"x-hub-signature-256": "sha256=deadbeef"})


def test_twilio_parses_audio():
    (m,) = TwilioAdapter().parse(
        {
            "From": "whatsapp:+919876543210",
            "MessageSid": "SM1",
            "NumMedia": "1",
            "MediaContentType0": "audio/ogg",
            "MediaUrl0": "https://api.twilio.com/media/1",
            "Body": "",
        },
        {},
    )
    assert m.kind == "audio" and m.media.url.endswith("/1")


@pytest.mark.asyncio
async def test_mock_adapter_records_sends():
    from app.channels.base import OutboundMessage

    a = MockAdapter()
    await a.send(OutboundMessage(to="91999", kind="text", text="hi"))
    assert a.sent[0].text == "hi"


def test_adapter_instance_is_shared_across_call_styles():
    from app.channels.whatsapp.adapters import get_adapter, reset_adapter_cache

    reset_adapter_cache()
    # A stateful adapter must not silently become two instances.
    assert get_adapter() is get_adapter("mock")
