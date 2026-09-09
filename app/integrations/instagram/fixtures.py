"""Fixtures returned while INSTAGRAM_MOCK is on.

Shapes match the real Graph responses field-for-field, so the day Track A
lands the only change is the flag.
"""

from datetime import UTC, datetime, timedelta

MOCK_TOKEN = {
    "access_token": "IGAA-mock-long-lived-token",
    "user_id": "17841400000000000",
    "token_type": "bearer",
    "expires_in": 60 * 24 * 60 * 60,
}

MOCK_PROFILE = {
    "user_id": "17841400000000000",
    "username": "mock_shop",
    "name": "Mock Shop",
    "account_type": "BUSINESS",
    "profile_picture_url": "https://example.invalid/pp.jpg",
    "followers_count": 1234,
    "media_count": 42,
}

MOCK_CONTAINER_ID = "18000000000000000"
_child_seq = [0]


def mock_child_container_id() -> str:
    _child_seq[0] += 1
    return f"1800000000000{_child_seq[0]:04d}"
MOCK_MEDIA_ID = "17900000000000000"
MOCK_PERMALINK = "https://www.instagram.com/p/MOCKSHORTCODE/"


def mock_token_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=MOCK_TOKEN["expires_in"])
