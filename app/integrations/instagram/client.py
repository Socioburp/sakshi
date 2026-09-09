"""Instagram publishing.

STUBBED until Track A. The four functions below are the entire surface the
rest of the codebase is allowed to touch; nothing outside this package knows
whether it is talking to Meta or to a fixture. Real implementations are
written and sit behind `settings.instagram_mock` -- flipping the flag is the
whole of the integration work once the app review clears.

Publishing is a two-step dance on Meta's side:
    1. POST /{ig-user-id}/media          -> container id   (Meta FETCHES image_url)
    2. POST /{ig-user-id}/media_publish  -> media id

Step 1 is why R2 must be publicly readable. Meta's fetcher is an anonymous
client on the open internet: a signed-URL-only bucket fails here, and it fails
at publish time rather than at upload time, which is the worst place to find out.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from app.config import settings
from app.integrations.instagram import fixtures
from app.logging import get_logger

log = get_logger(__name__)

GRAPH = "https://graph.instagram.com/v21.0"
OAUTH = "https://api.instagram.com/oauth/access_token"

SCOPES = [
    "instagram_business_basic",
    "instagram_business_content_publish",
]


@dataclass(slots=True)
class IgToken:
    access_token: str
    user_id: str
    expires_at: datetime


@dataclass(slots=True)
class IgProfile:
    user_id: str
    username: str
    name: str | None
    account_type: str | None
    followers_count: int | None
    media_count: int | None


@dataclass(slots=True)
class IgPublishResult:
    media_id: str
    permalink: str | None


def authorize_url(state: str) -> str:
    """Not one of the four; just the link we put in the WhatsApp reply."""
    from urllib.parse import urlencode

    q = urlencode(
        {
            "client_id": settings.ig_app_id,
            "redirect_uri": settings.ig_redirect_uri,
            "scope": ",".join(SCOPES),
            "response_type": "code",
            "state": state,
        }
    )
    return f"https://www.instagram.com/oauth/authorize?{q}"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30.0)


# --------------------------------------------------------------------------- #
# 1/4
# --------------------------------------------------------------------------- #
async def exchange_code_for_token(code: str) -> IgToken:
    if settings.instagram_mock:
        log.info("ig_mock", fn="exchange_code_for_token")
        t = fixtures.MOCK_TOKEN
        return IgToken(t["access_token"], t["user_id"], fixtures.mock_token_expiry())

    async with _client() as c:
        short = await c.post(
            OAUTH,
            data={
                "client_id": settings.ig_app_id,
                "client_secret": settings.ig_app_secret,
                "grant_type": "authorization_code",
                "redirect_uri": settings.ig_redirect_uri,
                "code": code,
            },
        )
        short.raise_for_status()
        s = short.json()
        long = await c.get(
            f"{GRAPH}/access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": settings.ig_app_secret,
                "access_token": s["access_token"],
            },
        )
        long.raise_for_status()
        lj = long.json()
        return IgToken(
            access_token=lj["access_token"],
            user_id=str(s.get("user_id") or s.get("permissions", {}).get("user_id", "")),
            expires_at=datetime.now(UTC) + timedelta(seconds=lj.get("expires_in", 5184000)),
        )


async def refresh_long_lived_token(access_token: str) -> IgToken:
    """Long-lived tokens last 60 days and can be refreshed once past 24h old.

    Nothing refreshed them before, so every connection silently died on day
    60 and the publish tool kept retrying a dead token.
    """
    if settings.instagram_mock:
        log.info("ig_mock", fn="refresh_long_lived_token")
        t = fixtures.MOCK_TOKEN
        return IgToken(t["access_token"], t["user_id"], fixtures.mock_token_expiry())

    async with _client() as c:
        r = await c.get(
            f"{GRAPH}/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": access_token},
        )
        r.raise_for_status()
        j = r.json()
        return IgToken(
            access_token=j["access_token"],
            user_id="",
            expires_at=datetime.now(UTC) + timedelta(seconds=j.get("expires_in", 5184000)),
        )


# Meta labels most Graph errors "OAuthException" -- invalid parameter, rate
# limit, bad aspect ratio -- so the type alone would disconnect an account
# over a rejected image. Only these codes mean the token itself is dead.
_DEAD_TOKEN_CODES = {190, 102}


def is_auth_error(exc: BaseException) -> bool:
    """True when Meta says the token is invalid or expired (error code 190/102)."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    if exc.response.status_code not in (400, 401):
        return False
    try:
        body = exc.response.json()
    except ValueError:
        return False
    err = body.get("error") if isinstance(body, dict) else None
    return isinstance(err, dict) and err.get("code") in _DEAD_TOKEN_CODES


# --------------------------------------------------------------------------- #
# 2/4
# --------------------------------------------------------------------------- #
async def get_profile(access_token: str) -> IgProfile:
    if settings.instagram_mock:
        log.info("ig_mock", fn="get_profile")
        p = fixtures.MOCK_PROFILE
        return IgProfile(
            p["user_id"],
            p["username"],
            p["name"],
            p["account_type"],
            p["followers_count"],
            p["media_count"],
        )

    async with _client() as c:
        r = await c.get(
            f"{GRAPH}/me",
            params={
                "fields": "user_id,username,name,account_type,followers_count,media_count",
                "access_token": access_token,
            },
        )
        r.raise_for_status()
        j = r.json()
        return IgProfile(
            user_id=str(j.get("user_id") or j.get("id")),
            username=j.get("username", ""),
            name=j.get("name"),
            account_type=j.get("account_type"),
            followers_count=j.get("followers_count"),
            media_count=j.get("media_count"),
        )


# --------------------------------------------------------------------------- #
# 3/4
# --------------------------------------------------------------------------- #
async def create_media_container(
    *,
    ig_user_id: str,
    access_token: str,
    image_url: str,
    caption: str = "",
    media_type: str = "IMAGE",
    alt_text: str | None = None,
    is_carousel_item: bool = False,
) -> str:
    """`image_url` must be publicly fetchable. Meta pulls it; we do not push it.

    A carousel child takes `is_carousel_item=True` and no caption -- the caption
    belongs to the parent container, and sending one on a child is silently
    ignored, which is worse than an error because the post ships captionless.
    """
    if settings.instagram_mock:
        log.info(
            "ig_mock",
            fn="create_media_container",
            image_url=image_url,
            is_carousel_item=is_carousel_item,
        )
        if is_carousel_item:
            return fixtures.mock_child_container_id()
        return fixtures.MOCK_CONTAINER_ID

    payload: dict[str, str] = {
        "image_url": image_url,
        "media_type": media_type,
        "access_token": access_token,
    }
    if is_carousel_item:
        payload["is_carousel_item"] = "true"
    else:
        payload["caption"] = caption[:2200]
    if alt_text:
        payload["alt_text"] = alt_text[:300]

    async with _client() as c:
        r = await c.post(f"{GRAPH}/{ig_user_id}/media", data=payload)
        r.raise_for_status()
        return r.json()["id"]


async def create_carousel_container(
    *, ig_user_id: str, access_token: str, children: list[str], caption: str
) -> str:
    """Parent container holding 2-10 already-created child containers."""
    if not 2 <= len(children) <= 10:
        raise ValueError(f"Instagram carousels take 2-10 slides, got {len(children)}")
    if settings.instagram_mock:
        log.info("ig_mock", fn="create_carousel_container", children=len(children))
        return fixtures.MOCK_CONTAINER_ID

    async with _client() as c:
        r = await c.post(
            f"{GRAPH}/{ig_user_id}/media",
            data={
                "media_type": "CAROUSEL",
                "children": ",".join(children),
                "caption": caption[:2200],
                "access_token": access_token,
            },
        )
        r.raise_for_status()
        return r.json()["id"]


async def wait_for_container(*, container_id: str, access_token: str, timeout_s: int = 60) -> str:
    """Poll status_code until FINISHED. Meta rejects publish on IN_PROGRESS."""
    if settings.instagram_mock:
        return "FINISHED"
    deadline = datetime.now(UTC) + timedelta(seconds=timeout_s)
    async with _client() as c:
        while datetime.now(UTC) < deadline:
            r = await c.get(
                f"{GRAPH}/{container_id}",
                params={"fields": "status_code,status", "access_token": access_token},
            )
            r.raise_for_status()
            j = r.json()
            if j.get("status_code") in ("FINISHED", "ERROR", "EXPIRED"):
                if j["status_code"] != "FINISHED":
                    raise RuntimeError(f"container {container_id}: {j.get('status')}")
                return "FINISHED"
            await asyncio.sleep(2)
    raise TimeoutError(f"container {container_id} not ready in {timeout_s}s")


# --------------------------------------------------------------------------- #
# 4/4
# --------------------------------------------------------------------------- #
async def publish_container(
    *, ig_user_id: str, access_token: str, container_id: str
) -> IgPublishResult:
    if settings.instagram_mock:
        log.info("ig_mock", fn="publish_container", container_id=container_id)
        return IgPublishResult(fixtures.MOCK_MEDIA_ID, fixtures.MOCK_PERMALINK)

    async with _client() as c:
        r = await c.post(
            f"{GRAPH}/{ig_user_id}/media_publish",
            data={"creation_id": container_id, "access_token": access_token},
        )
        r.raise_for_status()
        media_id = r.json()["id"]
        link = await c.get(
            f"{GRAPH}/{media_id}", params={"fields": "permalink", "access_token": access_token}
        )
        permalink = link.json().get("permalink") if link.status_code == 200 else None
        return IgPublishResult(media_id=media_id, permalink=permalink)


__all__ = [
    "exchange_code_for_token",
    "get_profile",
    "create_media_container",
    "create_carousel_container",
    "publish_container",
    "wait_for_container",
    "refresh_long_lived_token",
    "is_auth_error",
    "authorize_url",
    "IgToken",
    "IgProfile",
    "IgPublishResult",
    "SCOPES",
]
