#!/usr/bin/env python3
"""Always-on MQTT command subscriber and relay driver.

The counterpart to `sensor_reader.py`, which is one-shot and timer-driven. This
process owns three things the reader does not: the device's MQTT status (a
retained "online" plus a last-will "offline"), the relay GPIO pins, and the
local idempotency store.

The Pi is purely reactive. It never decides to water — it executes `water_now`
commands and reports what happened. Every safety rule it enforces is local and
unconditional, because the cloud is exactly the component that might be
compromised, buggy, or replaying an hours-old message:

  * a duration cap, so a bad `duration_sec` cannot empty the tank
  * an expiry check, so a command queued during an outage cannot fire late
  * a per-channel busy check, so a redelivery cannot double-water
  * a de-energised-at-startup GPIO init, so a restart cannot leave a pump on
  * a `finally` that de-energises, so a crash mid-watering still stops the pump
"""
import asyncio
import json
import logging
import os
import signal
import socket
import sqlite3
from datetime import UTC, datetime
from uuid import UUID, uuid4

import RPi.GPIO as GPIO
from aiomqtt import Client, MqttError, TLSParameters, Will
from relay import DE_ENERGISED, ENERGISED, RELAY_PINS

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("watering_controller")

DEVICE_ID = os.environ["DEVICE_ID"]
OUTBOX_DB = os.environ.get(
    "PI_OUTBOX_DB", "/home/kwhatever/plant-monitor/pi_client/executed_commands.db")

# Hard cap applied to every command regardless of what the cloud asked for.
MAX_WATERING_DURATION_SEC = int(os.environ.get("MAX_WATERING_DURATION_SEC", "120"))

# Tank lockout. 0 disables it, which is the current reality — no water-level
# sensor is wired (see migrations/002_seed.sql). Set both to enable: the
# channel is an MCP3008 input, e.g. TANK_ADC_CHANNEL=6.
TANK_MIN_PCT = float(os.environ.get("TANK_MIN_PCT", "0"))
TANK_ADC_CHANNEL = os.environ.get("TANK_ADC_CHANNEL")

# How many pumps may run at once. Default 1: the relay board and the pumps share
# one supply, and the plan never promised it can source six solenoids
# simultaneously. Commands beyond the limit queue rather than fail, since a
# watering is bounded by MAX_WATERING_DURATION_SEC — but expiry is re-checked
# after the wait, so a command that queued too long expires instead of firing.
MAX_CONCURRENT_RELAYS = int(os.environ.get("MAX_CONCURRENT_RELAYS", "1"))

EXECUTED_MAX_ROWS = 500     # bound disk; far more than the ~24 commands/day ceiling
TERMINAL_EVENTS = ("watering_completed", "watering_failed", "command_expired")

COMMANDS_TOPIC = f"plant-monitor/{DEVICE_ID}/commands"
EVENTS_TOPIC = f"plant-monitor/{DEVICE_ID}/events"
STATUS_TOPIC = f"plant-monitor/{DEVICE_ID}/status"

shutdown = asyncio.Event()

# Prevents concurrent GPIO activation from duplicate MQTT deliveries, reconnect
# replays, or Consumer bugs. The DB's UNIQUE (device_id, relay_channel) only
# stops a second command being *created*; it does nothing about the Pi being
# handed the same command twice.
active_relays: set[int] = set()

# Created inside main() — asyncio primitives must not bind a loop at import time.
pump_slots: asyncio.Semaphore | None = None
inflight: set[asyncio.Task] = set()


# --- local idempotency store -------------------------------------------------

def store() -> sqlite3.Connection:
    conn = sqlite3.connect(OUTBOX_DB, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    # Same database file as sensor_reader's reading_outbox, different table.
    # WAL allows its writer and this one to coexist; busy_timeout absorbs the
    # brief SQLITE_BUSY a checkpoint can still cause.
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS executed_commands (
            command_id  TEXT PRIMARY KEY,
            event       TEXT NOT NULL,
            payload     TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    return conn


def remember(conn, command_id: str, event: str, payload: dict) -> None:
    """Record a command's terminal outcome so a redelivery can be answered.

    OR IGNORE, not OR REPLACE: the FIRST terminal outcome is the true one and
    must never be overwritten. The path that makes this matter is real — if the
    broker drops while publishing `watering_completed`, the exception handler
    below publishes `watering_failed`, which would otherwise replace a genuine
    completion with a failure. A redelivery would then report that a watering
    which actually ran had failed, and the backend could water again.
    """
    conn.execute(
        "INSERT OR IGNORE INTO executed_commands(command_id, event, payload) "
        "VALUES (?, ?, ?)", (command_id, event, json.dumps(payload)))
    conn.execute("""
        DELETE FROM executed_commands
        WHERE rowid NOT IN (
            SELECT rowid FROM executed_commands ORDER BY created_at DESC LIMIT ?
        )
    """, (EXECUTED_MAX_ROWS,))


def recall(conn, command_id: str) -> dict | None:
    row = conn.execute(
        "SELECT payload FROM executed_commands WHERE command_id = ?",
        (command_id,)).fetchone()
    return json.loads(row[0]) if row else None


# --- message plumbing --------------------------------------------------------

def parse_command(raw: bytes | str) -> dict | None:
    """Validate a command payload, or return None to drop it.

    Deliberately strict about the fields that end up in an event: the Consumer
    validates `relay_channel` as 1-8 and `command_id` as a UUID, so echoing a
    malformed one back would just get the event dead-lettered, losing the only
    signal that something is wrong. Better to log it here where it is visible.
    """
    try:
        command = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("undecodable command payload dropped: %r", raw[:200])
        return None
    if not isinstance(command, dict):
        logger.warning("non-object command payload dropped: %r", command)
        return None

    missing = {"command_id", "plant_id", "relay_channel", "duration_sec",
               "expires_at"} - command.keys()
    if missing:
        logger.warning("command missing %s, dropped", sorted(missing))
        return None
    try:
        UUID(str(command["command_id"]))
        command["relay_channel"] = int(command["relay_channel"])
        command["plant_id"] = int(command["plant_id"])
        command["duration_sec"] = int(command["duration_sec"])
    except (ValueError, TypeError) as e:
        logger.warning("command has unusable field types (%s), dropped", e)
        return None
    if not 1 <= command["relay_channel"] <= 8:
        # Out of the schema's range entirely — an event naming it would be
        # rejected by the Consumer, so there is nothing useful to publish.
        logger.warning("command relay_channel %s outside 1-8, dropped",
                       command["relay_channel"])
        return None
    return command


def build_event(event: str, command: dict, *, error: str | None = None,
                duration_sec: int | None = None) -> dict:
    return {
        "schema_version": 2,
        "event_id": str(uuid4()),
        "event": event,
        "command_id": str(command["command_id"]),
        "device_id": DEVICE_ID,
        "plant_id": command["plant_id"],
        "relay_channel": command["relay_channel"],
        "duration_sec": duration_sec,
        "error": error,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    # triggered_by is absent from the command payload, so the Pi cannot echo it.
    # The Consumer's model has it optional and the DB already recorded it when
    # the command was created.


async def publish_event(client, conn, event: str, command: dict, **kw) -> None:
    payload = build_event(event, command, **kw)
    if event in TERMINAL_EVENTS:
        # Persist BEFORE publishing. If the broker drops between the two, the
        # stored outcome still answers a redelivery; publishing first and
        # crashing would leave the Pi ready to water the same plant again.
        remember(conn, payload["command_id"], event, payload)
    await client.publish(EVENTS_TOPIC, json.dumps(payload), qos=1)
    logger.info("published %s for command %s (relay %s)",
                event, payload["command_id"], payload["relay_channel"])


def expired(command: dict) -> bool:
    try:
        deadline = datetime.fromisoformat(command["expires_at"])
    except (ValueError, TypeError):
        logger.warning("unparseable expires_at %r, treating as expired",
                       command.get("expires_at"))
        return True
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return datetime.now(UTC) >= deadline


# --- hardware ----------------------------------------------------------------

async def tank_ok() -> tuple[bool, str | None]:
    """(is there water, error slug). Fails closed: an unreadable tank blocks."""
    if TANK_MIN_PCT <= 0:
        return True, None
    if TANK_ADC_CHANNEL is None:
        logger.warning("TANK_MIN_PCT set but TANK_ADC_CHANNEL is not; "
                       "cannot verify tank level")
        return False, "tank_unreadable"
    try:
        # Imported lazily for the same reason sensor_reader defers DHT22: this
        # is optional hardware, and the import opens the SPI bus.
        from sensors.adc import ADCSensor
        readings = await ADCSensor(channel=int(TANK_ADC_CHANNEL),
                                   sensor_type="water_level").read()
        level = readings[0]["value"]
    except Exception:
        # SPI can be busy — sensor_reader holds the bus during its timer tick.
        # Running a pump dry destroys it, so refuse rather than guess.
        logger.exception("tank level read failed")
        return False, "tank_unreadable"
    if level is None or level < TANK_MIN_PCT:
        return False, "tank_empty"
    return True, None


async def execute_relay(pin: int, duration: int) -> None:
    GPIO.output(pin, ENERGISED)
    try:
        await asyncio.sleep(duration)
    finally:
        GPIO.output(pin, DE_ENERGISED)


# --- command handling --------------------------------------------------------

async def handle_command(client, conn, command: dict) -> None:
    command_id = str(command["command_id"])
    channel = command["relay_channel"]

    # A command whose outcome is already known is answered from the store with a
    # fresh event_id, never re-executed. This is what makes QoS-1 redelivery and
    # broker session replay safe, and it resolves a command the backend still
    # shows as pending far faster than waiting for stale-expiry.
    previous = recall(conn, command_id)
    if previous is not None:
        replay = dict(previous, event_id=str(uuid4()),
                      recorded_at=datetime.now(UTC).isoformat())
        await client.publish(EVENTS_TOPIC, json.dumps(replay), qos=1)
        logger.info("command %s already terminal (%s), republished",
                    command_id, replay["event"])
        return

    if expired(command):
        await publish_event(client, conn, "command_expired", command)
        return

    if channel not in RELAY_PINS:
        # In 1-8 but not wired — channels 4 and 7. Explicit failure beats a
        # command that appears to succeed while no water moves.
        await publish_event(client, conn, "watering_failed", command,
                            error="invalid_relay_channel")
        return

    if channel in active_relays:
        # Reject rather than queue: the in-flight command owns this relay slot,
        # and this one is a duplicate delivery of it or a racing sibling.
        await publish_event(client, conn, "watering_failed", command,
                            error="relay_busy")
        return

    duration = max(1, min(command["duration_sec"], MAX_WATERING_DURATION_SEC))
    if duration != command["duration_sec"]:
        logger.warning("command %s asked for %ss, capped to %ss",
                       command_id, command["duration_sec"], duration)

    active_relays.add(channel)
    try:
        async with pump_slots:
            # Re-checked after the semaphore: queueing behind other pumps can
            # push a command past its own expiry, and firing then would water on
            # stale intent.
            if expired(command):
                await publish_event(client, conn, "command_expired", command)
                return

            ok, tank_error = await tank_ok()
            if not ok:
                await publish_event(client, conn, "watering_failed", command,
                                    error=tank_error)
                return

            await publish_event(client, conn, "watering_started", command,
                                duration_sec=duration)
            await asyncio.wait_for(
                execute_relay(RELAY_PINS[channel], duration),
                timeout=MAX_WATERING_DURATION_SEC + 5)
            await publish_event(client, conn, "watering_completed", command,
                                duration_sec=duration)
    except TimeoutError:
        await publish_event(client, conn, "watering_failed", command,
                            error="relay_timeout")
    except asyncio.CancelledError:
        # Shutdown mid-watering. execute_relay's finally already de-energised
        # the pin; re-raise so the drain in main() can see the task finish.
        raise
    except Exception:
        logger.exception("relay error on channel %s", channel)
        try:
            await publish_event(client, conn, "watering_failed", command,
                                error="relay_error")
        except MqttError:
            # Broker went away too. The outcome is in SQLite, so a redelivery
            # gets answered; otherwise stale-expiry resolves it backend-side.
            logger.error("could not publish failure for command %s", command_id)
    finally:
        # Belt and braces: execute_relay de-energises in its own finally, but
        # this also covers a crash before it was ever entered.
        GPIO.output(RELAY_PINS[channel], DE_ENERGISED)
        active_relays.discard(channel)


def spawn(coro) -> None:
    """Run a command handler concurrently, keeping a reference so it isn't GC'd.

    Handling inline would block the message loop for the whole watering, so a
    second plant's command could not even be parsed — let alone rejected — until
    the first finished.
    """
    task = asyncio.create_task(coro)
    inflight.add(task)
    task.add_done_callback(inflight.discard)


# --- MQTT session ------------------------------------------------------------

async def session(conn) -> None:
    tls = TLSParameters(ca_certs=os.environ["MQTT_CA_FILE"])
    will = Will(STATUS_TOPIC,
                payload=json.dumps({"schema_version": 2, "device_id": DEVICE_ID,
                                    "status": "offline"}),
                qos=1, retain=True)
    async with Client(
        os.environ["MQTT_HOST"],
        port=int(os.environ.get("MQTT_PORT", "8883")),
        username=os.environ["MQTT_USERNAME"],
        password=os.environ["MQTT_PASSWORD"],
        tls_params=tls,
        # Fixed identifier + clean_session=False: the broker queues commands
        # published while the Pi was down and delivers them on reconnect.
        # (aiomqtt 2.x renamed asyncio-mqtt's `client_id` to `identifier`.)
        identifier=f"watering_{DEVICE_ID}",
        clean_session=False,
        will=will,
    ) as client:
        await client.publish(STATUS_TOPIC, json.dumps({
            "schema_version": 2, "device_id": DEVICE_ID, "status": "online",
            "recorded_at": datetime.now(UTC).isoformat(),
            "capabilities": {"relay_channels": sorted(RELAY_PINS)},
        }), qos=1, retain=True)
        await client.subscribe(COMMANDS_TOPIC, qos=1)
        logger.info("connected to %s, subscribed to %s",
                    os.environ["MQTT_HOST"], COMMANDS_TOPIC)

        async for message in client.messages:
            if shutdown.is_set():
                break
            command = parse_command(message.payload)
            if command is not None:
                spawn(handle_command(client, conn, command))


async def run_with_reconnect(conn, *, base: float = 1.0, cap: float = 60.0) -> None:
    delay = base
    while not shutdown.is_set():
        try:
            await session(conn)
            delay = base
        except asyncio.CancelledError:
            raise
        except (MqttError, OSError) as e:
            logger.warning("mqtt disconnect: %s; reconnecting in %.1fs", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, cap)


# --- systemd integration -----------------------------------------------------

def sd_notify(state: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]     # abstract namespace
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(state.encode())
    except OSError as e:
        logger.debug("sd_notify(%s) failed: %s", state, e)


async def watchdog_pinger() -> None:
    """Keep systemd's watchdog fed from the event loop.

    The ping is a task on the same loop as everything else, which is the point:
    if the loop wedges — a hung GPIO call, a blocking library, a deadlock — the
    pings stop, systemd kills the unit and Restart=always brings it back. A
    thread-based ping would keep reporting healthy while nothing worked.
    """
    usec = os.environ.get("WATCHDOG_USEC")
    if not usec:
        return
    interval = int(usec) / 2_000_000    # microseconds -> seconds, then halved
    while not shutdown.is_set():
        await asyncio.sleep(interval)
        sd_notify("WATCHDOG=1")


# --- entrypoint --------------------------------------------------------------

def install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: shutdown.set())


def init_gpio() -> None:
    GPIO.setmode(GPIO.BCM)
    # De-energised BEFORE any MQTT connection exists, so a queued command cannot
    # arrive while the pins are still at their power-on levels. initial= sets the
    # level as part of setup; a bare setup() then output() leaves a window at the
    # default LOW, which on this active-low board is a real pump pulse.
    for pin in RELAY_PINS.values():
        GPIO.setup(pin, GPIO.OUT, initial=DE_ENERGISED)
    logger.info("relay pins initialised de-energised: %s", sorted(RELAY_PINS))


async def main() -> None:
    global pump_slots
    pump_slots = asyncio.Semaphore(MAX_CONCURRENT_RELAYS)

    init_gpio()
    install_signal_handlers(asyncio.get_running_loop())
    conn = store()

    mqtt_task = asyncio.create_task(run_with_reconnect(conn))
    watchdog_task = asyncio.create_task(watchdog_pinger())
    sd_notify("READY=1")
    logger.info("watering controller started")

    try:
        await shutdown.wait()
    finally:
        sd_notify("STOPPING=1")
        logger.info("shutting down: %d watering(s) in flight", len(inflight))
        mqtt_task.cancel()
        watchdog_task.cancel()
        await asyncio.gather(mqtt_task, watchdog_task, return_exceptions=True)
        # Give in-flight waterings a moment to finish and publish. They are
        # bounded by MAX_WATERING_DURATION_SEC; anything still running past that
        # gets cancelled, and execute_relay's finally stops its pump either way.
        if inflight:
            await asyncio.wait(inflight, timeout=MAX_WATERING_DURATION_SEC + 5)
            for task in list(inflight):
                task.cancel()
            await asyncio.gather(*inflight, return_exceptions=True)
        for pin in RELAY_PINS.values():
            GPIO.output(pin, DE_ENERGISED)
        GPIO.cleanup()
        conn.close()
        logger.info("watering controller stopped")


if __name__ == "__main__":
    asyncio.run(main())
