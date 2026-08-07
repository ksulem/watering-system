"""Pydantic models for every MQTT payload the Consumer accepts.

Nothing reaches a handler as a raw dict: `main.dispatch` validates against one
of these models first and dead-letters anything that fails. This is the trust
boundary — malformed firmware output, partial writes, and hostile payloads stop
here instead of poisoning `processed_sensor_events` or crashing a task.
"""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, NonNegativeInt

SUPPORTED_SCHEMA_VERSIONS = {2}


class ReadingMessage(BaseModel):
    schema_version: int
    reading_id: UUID
    device_id: UUID
    address: str = Field(min_length=1, max_length=50)
    value: float | None
    raw_adc: NonNegativeInt | None = None
    sensor_type: str = Field(min_length=1, max_length=50)
    recorded_at: datetime


class WateringEventMessage(BaseModel):
    schema_version: int
    event_id: UUID
    # watering_started is included even though it carries no terminal outcome:
    # it's how `watering_events.started_at` gets the real relay-execution time
    # rather than broker-delivery time. Omitting it here (as an earlier draft of
    # the plan did) would dead-letter every started event and leave started_at
    # permanently NULL.
    event: Literal[
        "watering_started", "watering_completed", "watering_failed", "command_expired"
    ]
    command_id: UUID
    device_id: UUID
    plant_id: int
    relay_channel: int = Field(ge=1, le=8)
    duration_sec: NonNegativeInt | None = None
    triggered_by: Literal["auto", "manual", "bot"] | None = None
    error: str | None = None
    recorded_at: datetime


class StatusMessage(BaseModel):
    schema_version: int
    device_id: UUID
    status: Literal["online", "offline", "degraded"]
    # The last-will `offline` payload is pre-baked at connect time and fired by
    # the broker at an unknown later moment, so it deliberately carries no
    # timestamp — the handler substitutes now() when this is None.
    recorded_at: datetime | None = None
    # Sent only in the `online` message; older firmware omits them entirely.
    firmware_version: str | None = Field(default=None, max_length=50)
    boot_at: datetime | None = None
    capabilities: dict | None = None
