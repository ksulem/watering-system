"""Minimal aiohttp server exposing /health and /metrics on port 8002.

Without /health, compose's healthcheck can never pass and the service is
permanently marked unhealthy — which in turn blocks anything that declares
`depends_on: condition: service_healthy` on it.
"""
import logging

from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from consumer import db

logger = logging.getLogger("consumer.health")

PORT = 8002


async def _health(request: web.Request) -> web.Response:
    ok = await db.healthy()
    # Liveness alone isn't enough: a Consumer that can't reach TimescaleDB is
    # dropping every reading, so report unhealthy and let Docker restart it.
    return web.json_response({"status": "ok" if ok else "degraded", "db": ok},
                             status=200 if ok else 503)


async def _metrics(request: web.Request) -> web.Response:
    # Content-Type is set via headers, not the content_type kwarg: aiohttp
    # rejects a content_type containing a charset parameter, and
    # CONTENT_TYPE_LATEST carries one.
    return web.Response(body=generate_latest(),
                        headers={"Content-Type": CONTENT_TYPE_LATEST})


async def start_health_server(port: int = PORT) -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/health", _health)
    app.router.add_get("/metrics", _metrics)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info("health server listening on :%d (/health, /metrics)", port)
    return runner
