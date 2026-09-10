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
    checks["env"] = settings.env
    ok = all(v in ("ok", "set") for k, v in checks.items() if k != "env")
    return {"ok": ok, "checks": checks}
