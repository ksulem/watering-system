"""Background maintenance: expire stale commands, prune unbounded tables.

TimescaleDB retention policies cover `sensor_readings` only. Everything else
here grows forever without pruning.
"""
import asyncio
import logging

from consumer import db, metrics

logger = logging.getLogger("consumer.cleanup")

CLEANUP_INTERVAL = 60          # seconds between passes
GAUGE_INTERVAL = 15            # seconds between outbox gauge refreshes


async def expire_stale_commands() -> None:
    # Expire against the same expires_at the Pi sees, not a hardcoded age, so
    # the DB can't diverge from Pi reality for commands with a non-default TTL.
    #
    # The 130s grace = MAX_WATERING_DURATION_SEC (120) + 10s margin. The Pi
    # checks expires_at only BEFORE energising the relay, so a watering that
    # starts 1s inside the window legitimately finishes up to ~120s later.
    # Expiring exactly at expires_at would race that run: 'expired' is
    # absorbing, so the real watering_completed could not correct it, the
    # cooldown check would miss a genuine watering, and auto-water would fire
    # again — watering the plant twice.
    result = await db.execute("""
        UPDATE watering_events
        SET status = 'expired', completed_at = now()
        WHERE status IN ('pending', 'dispatched')
          AND expires_at + INTERVAL '130 seconds' < now()
    """)
    if result and result != "UPDATE 0":
        logger.info("expired stale watering commands: %s", result)


async def prune_tables() -> None:
    # ~8 rows/min at steady state = ~4M rows/year.
    await db.execute("""
        DELETE FROM processed_sensor_events
        WHERE processed_at < now() - INTERVAL '30 days'
    """)
    # Terminal outbox rows are kept 30 days for audit, then dropped.
    await db.execute("""
        DELETE FROM command_outbox
        WHERE status IN ('dispatched', 'failed')
          AND created_at < now() - INTERVAL '30 days'
    """)
    await db.execute("""
        DELETE FROM alerts
        WHERE acknowledged = true AND sent_at < now() - INTERVAL '90 days'
    """)
    # If a malformed-payload bug is still unfixed after 30 days, the
    # field-debug window has long closed.
    await db.execute("""
        DELETE FROM dead_letters
        WHERE created_at < now() - INTERVAL '30 days'
    """)


async def run() -> None:
    """Cleanup loop. Never dies on a transient DB error — the next pass retries."""
    ticks = 0
    while True:
        try:
            metrics.command_outbox_pending.set(await db.count_pending_outbox())
            # Expiry and pruning are far cheaper than the gauge is useful, so
            # they run on a slower cadence than the gauge refresh.
            if ticks % (CLEANUP_INTERVAL // GAUGE_INTERVAL) == 0:
                await expire_stale_commands()
                await prune_tables()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("cleanup pass failed; retrying next tick")
        ticks += 1
        await asyncio.sleep(GAUGE_INTERVAL)
