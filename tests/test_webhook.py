from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

client = TestClient(app)


def test_health():
    assert client.get("/health").json()["ok"] is True


def test_webhook_handshake_echoes_challenge():
    r = client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": settings.wa_verify_token,
            "hub.challenge": "CHALLENGE-42",
        },
    )
    assert r.status_code == 200
    assert r.text == "CHALLENGE-42"
    assert r.headers["content-type"].startswith("text/plain")


def test_webhook_handshake_rejects_bad_token():
    r = client.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "x"},
    )
    assert r.status_code == 403
