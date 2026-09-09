from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from app.billing.router import router as billing_router
from app.channels.whatsapp.router import router as whatsapp_router
from app.config import settings
from app.db.session import engine
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
app.include_router(billing_router)


@app.get("/health")
async def health() -> dict:
    """Liveness only. Render pings this; it must not depend on Postgres."""
    return {"ok": True, "service": "sakshi", "env": settings.env}


@app.get("/health/ready")
async def ready() -> dict:
    """Readiness: the things a turn actually needs."""
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
    checks["anthropic_model"] = "set" if settings.anthropic_model else "MISSING"
    checks["r2_public_base"] = "set" if settings.r2_public_base_url else "MISSING"
    ok = all(v in ("ok", "set") for v in checks.values())
    return {"ok": ok, "checks": checks}
