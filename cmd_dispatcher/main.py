import asyncio
import json
import logging
import os
from datetime import timedelta

import asyncpg
from aiomqtt import Client as MQTTClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cmd_dispatcher")

POLL_INTERVAL = 2       # seconds between outbox polls
MAX_ATTEMPTS = 3
BACKOFF_BASE = 10       # seconds; doubled per attempt


async def create_pool_with_retry(dsn: str, *, min_size: int, max_size: int,
                                  retry_delay: int = 5) -> asyncpg.Pool:
    # Dispatcher and TimescaleDB are separate containers - depends_on's
    # service_healthy only guarantees the DB accepted its own healthcheck,
    # not that it's still accepting new connections by the time this
    # process's first connection attempt lands. Retry rather than crash-loop.
    while True:
        try:
            return await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
        except (OSError, asyncpg.PostgresError) as e:
            logger.warning("DB not ready yet (%s), retrying in %ds", e, retry_delay)
            await asyncio.sleep(retry_delay)


async def dispatch_loop(pool: asyncpg.Pool, mqtt: MQTTClient) -> None:
    while True:
        # Each iteration acquires its own connection from the pool. `FOR UPDATE
        # SKIP LOCKED` is defensive: it makes the dispatcher safe to run with
        # multiple instances (rolling restarts, future scale-out) without
        # double-publishing commands. The transaction holds the row lock until
        # the row is updated to 'dispatched' (or its backoff bumped), so a
        # concurrent dispatcher silently skips it.
        async with pool.acquire() as db, db.transaction():
            rows = await db.fetch("""
                SELECT id, command_id, device_id, topic, payload, attempts
                FROM command_outbox
                WHERE status = 'pending' AND next_attempt_at <= now()
                ORDER BY created_at
                LIMIT 20
                FOR UPDATE SKIP LOCKED
            """)
            for row in rows:
                try:
                    # retain=False is mandatory — a retained water_now would be
                    # redelivered to the Pi on every reconnect and trigger watering
                    # for as long as the topic exists on the broker.
                    await mqtt.publish(row["topic"], json.dumps(dict(row["payload"])),
                                        qos=1, retain=False)
                    # Update both tables in the same transaction — watering_events
                    # is the canonical lifecycle table queried by the API;
                    # command_outbox is dispatch state.
                    await db.execute("""
                        UPDATE command_outbox
                        SET status = 'dispatched', dispatched_at = now()
                        WHERE id = $1
                    """, row["id"])
                    await db.execute("""
                        UPDATE watering_events
                        SET status = 'dispatched'
                        WHERE command_id = $1 AND status = 'pending'
                    """, row["command_id"])
                    # started_at is intentionally NOT set here — the Dispatcher only
                    # knows the command reached the broker (dispatched), not that the
                    # relay actually fired. Pi sets started_at by publishing a
                    # watering_started event; Consumer writes it via update_watering_event.
                except Exception:
                    logger.exception("dispatch failed for command %s", row["command_id"])
                    attempts = row["attempts"] + 1
                    if attempts >= MAX_ATTEMPTS:
                        # Mirror failure into watering_events so the API/UI don't show a stuck command.
                        await db.execute("""
                            UPDATE command_outbox SET status = 'failed', attempts = $1
                            WHERE id = $2
                        """, attempts, row["id"])
                        await db.execute("""
                            UPDATE watering_events
                            SET status = 'failed', completed_at = now()
                            WHERE command_id = $1 AND status IN ('pending', 'dispatched')
                        """, row["command_id"])
                    else:
                        backoff = timedelta(seconds=BACKOFF_BASE * (2 ** attempts))
                        await db.execute("""
                            UPDATE command_outbox
                            SET attempts = $1, next_attempt_at = now() + $2
                            WHERE id = $3
                        """, attempts, backoff, row["id"])
        await asyncio.sleep(POLL_INTERVAL)


async def main() -> None:
    # Pool, not single connection — even with one dispatcher instance, holding a
    # row lock across an MQTT publish on a single shared connection would block
    # health checks and queries from the same process.
    pool = await create_pool_with_retry(
        os.environ["DATABASE_URL"], min_size=2, max_size=5)
    async with MQTTClient(
        os.environ["MQTT_HOST"], port=int(os.environ.get("MQTT_PORT", "1883")),
        username=os.environ["MQTT_DISPATCHER_USERNAME"],
        password=os.environ["MQTT_DISPATCHER_PASSWORD"],
    ) as mqtt:
        logger.info("Command Dispatcher started, polling command_outbox every %ds", POLL_INTERVAL)
        await dispatch_loop(pool, mqtt)


if __name__ == "__main__":
    asyncio.run(main())
