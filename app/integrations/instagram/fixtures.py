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


# --------------------------------------------------------------------------- #
# insights
# --------------------------------------------------------------------------- #
# Seven posts over the last two months, shaped like /{ig-user-id}/media rows.
# `days_ago` becomes a real `timestamp` at call time so the fixture never
# ages out of the 90-day window. The newest is the id publish_container
# returns, so a post made here links back to its Publication and its brief.
# The numbers tell one story on purpose: the reel reaches most, the carousel
# is saved most, the Friday-evening offer converts, the plain Tuesday post
# does least -- what a real small shop's insights tend to look like.
_MOCK_MEDIA: list[dict] = [
    {
        "id": MOCK_MEDIA_ID,
        "media_type": "IMAGE",
        "media_product_type": "FEED",
        "days_ago": 2,
        "hour": 13,
        "caption": "Fresh batch pressed this morning",
        "like_count": 41,
        "comments_count": 3,
    },
    {
        "id": "17900000000000101",
        "media_type": "VIDEO",
        "media_product_type": "REELS",
        "days_ago": 6,
        "hour": 13,
        "caption": "How we press it, start to finish",
        "like_count": 188,
        "comments_count": 14,
    },
    {
        "id": "17900000000000102",
        "media_type": "CAROUSEL_ALBUM",
        "media_product_type": "FEED",
        "days_ago": 12,
        "hour": 13,
        "caption": "3 ways to use groundnut oil in everyday cooking",
        "like_count": 96,
        "comments_count": 9,
    },
    {
        "id": "17900000000000103",
        "media_type": "IMAGE",
        "media_product_type": "FEED",
        "days_ago": 16,
        "hour": 13,
        "caption": "Weekend special: 10% off on 1L packs",
        "like_count": 122,
        "comments_count": 11,
    },
    {
        "id": "17900000000000104",
        "media_type": "IMAGE",
        "media_product_type": "FEED",
        "days_ago": 23,
        "hour": 5,
        "caption": "Back in stock",
        "like_count": 22,
        "comments_count": 1,
    },
    {
        "id": "17900000000000105",
        "media_type": "IMAGE",
        "media_product_type": "FEED",
        "days_ago": 31,
        "hour": 13,
        "caption": "From our customers",
        "like_count": 58,
        "comments_count": 6,
    },
    {
        "id": "17900000000000106",
        "media_type": "IMAGE",
        "media_product_type": "FEED",
        "days_ago": 45,
        "hour": 5,
        "caption": "Now open till 9pm",
        "like_count": 30,
        "comments_count": 2,
    },
]

_MOCK_INSIGHTS: dict[str, dict[str, int]] = {
    MOCK_MEDIA_ID: {
        "reach": 640,
        "views": 700,
        "likes": 41,
        "comments": 3,
        "saved": 6,
        "shares": 2,
        "follows": 1,
        "profile_visits": 5,
        "total_interactions": 52,
    },
    "17900000000000101": {
        "reach": 2900,
        "views": 4100,
        "likes": 188,
        "comments": 14,
        "saved": 31,
        "shares": 44,
        "total_interactions": 277,
    },
    "17900000000000102": {
        "reach": 1250,
        "views": 1400,
        "likes": 96,
        "comments": 9,
        "saved": 58,
        "shares": 12,
        "follows": 4,
        "profile_visits": 19,
        "total_interactions": 175,
    },
    "17900000000000103": {
        "reach": 1480,
        "views": 1600,
        "likes": 122,
        "comments": 11,
        "saved": 14,
        "shares": 21,
        "follows": 6,
        "profile_visits": 33,
        "total_interactions": 168,
    },
    "17900000000000104": {
        "reach": 380,
        "views": 400,
        "likes": 22,
        "comments": 1,
        "saved": 2,
        "shares": 1,
        "follows": 0,
        "profile_visits": 3,
        "total_interactions": 26,
    },
    "17900000000000105": {
        "reach": 820,
        "views": 900,
        "likes": 58,
        "comments": 6,
        "saved": 9,
        "shares": 7,
        "follows": 2,
        "profile_visits": 8,
        "total_interactions": 80,
    },
    "17900000000000106": {
        "reach": 450,
        "views": 480,
        "likes": 30,
        "comments": 2,
        "saved": 3,
        "shares": 1,
        "follows": 0,
        "profile_visits": 4,
        "total_interactions": 36,
    },
}


def mock_media(now: datetime | None = None) -> list[dict]:
    """Rows shaped like the /media edge, newest first, timestamps relative to now."""
    now = now or datetime.now(UTC)
    out = []
    for m in _MOCK_MEDIA:
        when = (now - timedelta(days=m["days_ago"])).replace(
            hour=m["hour"], minute=30, second=0, microsecond=0
        )
        row = {k: v for k, v in m.items() if k not in ("days_ago", "hour")}
        row["timestamp"] = when.strftime("%Y-%m-%dT%H:%M:%S%z")
        row["permalink"] = f"https://www.instagram.com/p/MOCK{m['id'][-4:]}/"
        out.append(row)
    return out


def mock_media_insights(media_id: str) -> dict[str, int]:
    return dict(_MOCK_INSIGHTS.get(media_id) or {"reach": 100, "likes": 5, "comments": 0})


def mock_account_insights(since, until) -> dict[str, int]:
    days = max(1, (until - since).days + 1)
    return {"reach": 210 * days, "accounts_engaged": 26 * days, "total_interactions": 40 * days}
