from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import text

from app.billing.router import router as billing_router
from app.channels.whatsapp.router import router as whatsapp_router
from app.config import settings
from app.db.session import engine
from app.inbox.router import router as ig_webhook_router
from app.integrations.instagram.oauth import router as instagram_router
from app.logging import configure_logging, get_logger

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    log.info(
        "boot",
        env=settings.env,
        wa_provider=settings.wa_provider,
        stt=settings.stt_provider,
        imagegen=settings.imagegen_provider,
        instagram_mock=settings.instagram_mock,
    )
    yield
    from app.creative import compose

    await compose.shutdown()


app = FastAPI(title="Sakshi", version="0.2.0", lifespan=lifespan)
app.include_router(whatsapp_router)
app.include_router(instagram_router)
app.include_router(ig_webhook_router)
app.include_router(billing_router)


@app.get("/health")
async def health() -> dict:
    """Liveness only. Render pings this; it must not depend on Postgres."""
    return {"ok": True, "service": "sakshi", "env": settings.env}


def _probe() -> dict[str, str]:
    """Sync probes, run off the event loop. With Neon unreachable these block
    for the full connect timeout, and on the loop that meant every webhook
    timed out with them."""
    checks: dict[str, str] = {}
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        checks["postgres"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["postgres"] = f"fail: {exc}"[:120]
    try:
        from app.queue.client import get_redis

        get_redis().ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"fail: {exc}"[:120]
    return checks


# Which setting each picture vendor needs. A vendor with no key cannot make a
# single image, and nothing else in this probe would notice.
_IMAGEGEN_KEY = {
    "fal": "fal_key",
    "replicate": "replicate_api_token",
    "bfl": "bfl_api_key",
    "openai": "openai_api_key",
}


@app.get("/health/ready")
async def ready() -> dict:
    """Readiness: the things a turn actually needs."""
    checks = await run_in_threadpool(_probe)
    checks["anthropic_model"] = "set" if settings.anthropic_model else "MISSING"
    checks["r2_public_base"] = "set" if settings.r2_public_base_url else "MISSING"
    # Without it, memory writes are refused in prod (see app/memory/embed.py).
    checks["voyage_api_key"] = "set" if settings.voyage_api_key else "MISSING"
    if not settings.instagram_mock:
        # The OAuth state is signed with IG_APP_SECRET (WA_APP_SECRET as fallback).
        checks["ig_state_secret"] = (
            "set" if (settings.ig_app_secret or settings.wa_app_secret) else "MISSING"
        )
        checks["ig_app_id"] = "set" if settings.ig_app_id else "MISSING"
        checks["ig_redirect_uri"] = "set" if settings.ig_redirect_uri else "MISSING"
    # The picture is the product, and this probe used to say "ready" while the
    # service could not make one: a missing OPENAI_API_KEY, or a provider still
    # left on "mock", showed up only as an owner waiting for an image that was
    # never coming. Name it here so one request answers "why are there no
    # pictures" instead of a log hunt.
    checks["imagegen_provider"] = settings.imagegen_provider
    key = _IMAGEGEN_KEY.get(settings.imagegen_provider)
    if key is not None:
        checks["imagegen_key"] = "set" if getattr(settings, key, "") else "MISSING"
    checks["env"] = settings.env
    ok = all(v in ("ok", "set") for k, v in checks.items() if k not in ("env", "imagegen_provider"))
    # "mock" draws a gradient for development. In production it means every
    # client is being sent a placeholder, which is not a picture at all.
    if settings.env == "prod" and settings.imagegen_provider == "mock":
        ok = False
        checks["imagegen_provider"] = "mock: NOT A REAL PICTURE PROVIDER"
    return {"ok": ok, "checks": checks}
