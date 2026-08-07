"""FastAPI app for the Plant Monitor.

Single-user by design: there is no user table and no registration. A Telegram
Mini App exchanges initData for a session token once, then presents it as a
bearer token on every other endpoint.
"""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api import config, db
from api.routers import alerts, auth, plants, readings, sensors, watering

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_pool(config.DATABASE_URL)
    if not config.auth_configured():
        logger.warning(
            "Telegram auth is not configured (BOT_TOKEN / OWNER_USER_ID / "
            "SESSION_SECRET) — /auth/session returns 503 and every other "
            "endpoint is locked.")
    logger.info("api started")
    yield
    await db.close_pool()
    logger.info("api stopped")


app = FastAPI(title="Plant Monitor API", version="1.0.0", lifespan=lifespan)

# No origins configured means no cross-origin requests are permitted, which is
# the right default while nothing serves a browser front-end yet. Set
# CORS_ORIGINS once the Mini App has an address.
if config.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_methods=["GET", "POST", "PATCH", "PUT"],
        allow_headers=["X-Telegram-Init-Data", "Authorization"],
    )


@app.get("/health", tags=["health"])
async def health():
    """Unauthenticated liveness+readiness probe for Docker.

    Reports unhealthy when the DB is unreachable: an API that can't read
    TimescaleDB has nothing to serve, so a restart is the right response.
    """
    ok = await db.healthy()
    return JSONResponse({"status": "ok" if ok else "degraded", "db": ok},
                        status_code=200 if ok else 503)


app.include_router(auth.router)
app.include_router(plants.router)
app.include_router(readings.router)
app.include_router(watering.router)
app.include_router(sensors.router)
app.include_router(alerts.router)
