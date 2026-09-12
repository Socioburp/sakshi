"""Instagram Insights: what each post did, and what the account did.

Behind `settings.instagram_mock` like the publishing client, and with the
same shape rule: fixtures mirror the Graph responses field for field, so the
day the permission clears the only change is the flag.

The real calls target the Instagram API with Instagram Login
(graph.instagram.com), which has had media and user insights since
21 January 2025. They need the `instagram_business_manage_insights`
permission, which App Review grants; until then `requested_scopes()` leaves
it out so the login dialog does not fail on an unapproved scope.

Metric names follow the v22+ rules: `views` replaced `plays` and
`impressions` (both deprecated 21 April 2025); `saved`, `shares`, `reach`,
`likes`, `comments` and `total_interactions` exist on every post; `follows`
and `profile_visits` on feed posts only. Numbers can lag by up to 48 hours,
which is why a post is re-read while it is young.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from app.config import settings
from app.integrations.instagram import fixtures
from app.integrations.instagram.client import (
    GRAPH,
    INSIGHTS_SCOPE,
    _client,
    is_auth_error,
    requested_scopes,
)
from app.logging import get_logger

log = get_logger(__name__)

# Per-post metrics by product type. `follows`/`profile_visits` are refused
# on reels; a refused metric fails the whole call, hence the split.
FEED_METRICS = (
    "reach",
    "views",
    "likes",
    "comments",
    "saved",
    "shares",
    "follows",
    "profile_visits",
    "total_interactions",
)
REELS_METRICS = ("reach", "views", "likes", "comments", "saved", "shares", "total_interactions")
# The set every post type has accepted so far; the fallback when a 400 names
# a metric this media does not support (an old post, an album child, a story).
CORE_METRICS = ("reach", "likes", "comments", "saved", "shares", "total_interactions")
# Account-level, `period=day` with `metric_type=total_value`; at most 30 days
# per request. `follower_count` is left out: it needs 100 followers and the
# profile call already returns followers_count without any insights scope.
ACCOUNT_METRICS = ("reach", "accounts_engaged", "total_interactions")

MEDIA_FIELDS = (
    "id,media_type,media_product_type,timestamp,permalink,caption,like_count,comments_count"
)
PAGE_SIZE = 25
MAX_PAGES = 3


def can_read_insights(scopes: list[str] | None) -> bool:
    return settings.instagram_mock or INSIGHTS_SCOPE in (scopes or [])


@dataclass(slots=True)
class IgMedia:
    id: str
    media_type: str = "IMAGE"  # IMAGE | CAROUSEL_ALBUM | VIDEO
    media_product_type: str | None = None  # FEED | REELS | STORY
    timestamp: datetime | None = None
    permalink: str | None = None
    caption: str | None = None
    like_count: int | None = None
    comments_count: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_reel(self) -> bool:
        return (self.media_product_type or "").upper() == "REELS"

    @property
    def kind(self) -> str:
        """single | carousel | reel -- the word the rest of the product uses."""
        if self.is_reel:
            return "reel"
        if (self.media_type or "").upper() == "CAROUSEL_ALBUM":
            return "carousel"
        return "single"


def parse_ts(value: Any) -> datetime | None:
    """Graph timestamps look like 2025-07-02T10:30:00+0000."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    s = str(value)
    for parse in (datetime.fromisoformat, lambda v: datetime.strptime(v, "%Y-%m-%dT%H:%M:%S%z")):
        try:
            dt = parse(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _media_from(j: dict[str, Any]) -> IgMedia:
    return IgMedia(
        id=str(j.get("id", "")),
        media_type=str(j.get("media_type") or "IMAGE"),
        media_product_type=j.get("media_product_type"),
        timestamp=parse_ts(j.get("timestamp")),
        permalink=j.get("permalink"),
        caption=j.get("caption"),
        like_count=j.get("like_count"),
        comments_count=j.get("comments_count"),
        raw=j,
    )


async def list_media(
    *,
    access_token: str,
    ig_user_id: str,
    since: datetime | None = None,
    limit: int = 30,
) -> list[IgMedia]:
    """Newest first. Stops at `limit` posts or the first one older than `since`."""
    if settings.instagram_mock:
        log.info("ig_mock", fn="list_media")
        rows = [_media_from(m) for m in fixtures.mock_media()]
    else:
        rows = []
        url: str | None = f"{GRAPH}/{ig_user_id}/media"
        params: dict[str, Any] | None = {
            "fields": MEDIA_FIELDS,
            "limit": PAGE_SIZE,
            "access_token": access_token,
        }
        async with _client() as c:
            for _ in range(MAX_PAGES):
                if not url:
                    break
                r = await c.get(url, params=params)
                r.raise_for_status()
                j = r.json()
                page = [_media_from(m) for m in j.get("data") or []]
                rows.extend(page)
                oldest = page[-1].timestamp if page and page[-1].timestamp else None
                if len(rows) >= limit or not page or (since and oldest and oldest < since):
                    break
                url = ((j.get("paging") or {}).get("next")) or None
                params = None  # `next` carries the token and cursor
    out = []
    for m in rows:
        if not m.id or (m.media_product_type or "").upper() == "STORY":
            continue  # stories live 24h; their numbers say nothing about the grid
        if since and m.timestamp and m.timestamp < since:
            continue
        out.append(m)
        if len(out) >= limit:
            break
    return out


def _values(payload: dict[str, Any]) -> dict[str, int]:
    """Flatten a Graph insights reply into {metric: integer}."""
    out: dict[str, int] = {}
    for item in payload.get("data") or []:
        name = item.get("name")
        if not name:
            continue
        value: Any = None
        tv = item.get("total_value")
        if isinstance(tv, dict) and "value" in tv:
            value = tv["value"]
        else:
            vals = item.get("values") or []
            if vals:
                # period=day time series: one entry per day; sum them.
                nums = [v.get("value") for v in vals if isinstance(v, dict)]
                nums = [n for n in nums if isinstance(n, int | float)]
                value = sum(nums) if len(nums) > 1 else (nums[0] if nums else None)
        if isinstance(value, int | float):
            out[str(name)] = int(value)
    return out


async def media_insights(*, access_token: str, media: IgMedia) -> dict[str, int]:
    """Per-post numbers. Never raises for an unsupported metric: falls back
    to the core set, and to the counts on the media object itself."""
    if settings.instagram_mock:
        log.info("ig_mock", fn="media_insights", media_id=media.id)
        got = dict(fixtures.mock_media_insights(media.id))
    else:
        metrics = REELS_METRICS if media.is_reel else FEED_METRICS
        got = await _insights_call(
            access_token=access_token, media_id=media.id, metrics=metrics, retry_core=True
        )
    if "likes" not in got and media.like_count is not None:
        got["likes"] = int(media.like_count)
    if "comments" not in got and media.comments_count is not None:
        got["comments"] = int(media.comments_count)
    return got


async def _insights_call(
    *, access_token: str, media_id: str, metrics: tuple[str, ...], retry_core: bool
) -> dict[str, int]:
    async with _client() as c:
        r = await c.get(
            f"{GRAPH}/{media_id}/insights",
            params={"metric": ",".join(metrics), "access_token": access_token},
        )
        if r.status_code == 400 and retry_core and metrics != CORE_METRICS:
            # "(#100) ... metric[N] must be one of ..." -- an older post or a
            # type that lacks one of the richer metrics. Ask for the core set.
            log.info("ig_insights_metric_fallback", media_id=media_id)
            return await _insights_call(
                access_token=access_token,
                media_id=media_id,
                metrics=CORE_METRICS,
                retry_core=False,
            )
        r.raise_for_status()
        return _values(r.json())


async def account_insights(
    *, access_token: str, ig_user_id: str, since: date, until: date
) -> dict[str, int]:
    """Account totals for [since, until], at most 30 days."""
    if settings.instagram_mock:
        log.info("ig_mock", fn="account_insights")
        return dict(fixtures.mock_account_insights(since, until))
    if (until - since).days > 30:
        since = until - timedelta(days=30)
    start = datetime(since.year, since.month, since.day, tzinfo=UTC)
    end = datetime(until.year, until.month, until.day, tzinfo=UTC) + timedelta(days=1)
    async with _client() as c:
        r = await c.get(
            f"{GRAPH}/{ig_user_id}/insights",
            params={
                "metric": ",".join(ACCOUNT_METRICS),
                "period": "day",
                "metric_type": "total_value",
                "since": int(start.timestamp()),
                "until": int(end.timestamp()) - 1,
                "access_token": access_token,
            },
        )
        r.raise_for_status()
        return _values(r.json())


async def gather_media(
    *, access_token: str, media: list[IgMedia], concurrency: int = 4
) -> dict[str, dict[str, int] | BaseException]:
    """Insights for many posts, a few at a time. Errors are returned, not raised,
    except an auth error, which is raised at once: a dead token fails every call."""
    sem = asyncio.Semaphore(concurrency)

    async def one(m: IgMedia):
        async with sem:
            try:
                return m.id, await media_insights(access_token=access_token, media=m)
            except Exception as exc:  # noqa: BLE001 - one bad post must not lose the rest
                if is_auth_error(exc):
                    raise
                if isinstance(exc, httpx.HTTPStatusError):
                    log.warning(
                        "ig_media_insights_failed", media_id=m.id, status=exc.response.status_code
                    )
                else:
                    log.warning("ig_media_insights_failed", media_id=m.id, error=str(exc)[:120])
                return m.id, exc

    results = await asyncio.gather(*(one(m) for m in media))
    return dict(results)


__all__ = [
    "ACCOUNT_METRICS",
    "CORE_METRICS",
    "FEED_METRICS",
    "INSIGHTS_SCOPE",
    "IgMedia",
    "REELS_METRICS",
    "account_insights",
    "can_read_insights",
    "gather_media",
    "list_media",
    "media_insights",
    "parse_ts",
    "requested_scopes",
]
