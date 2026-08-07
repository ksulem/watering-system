"""Request and response models.

Request models mirror the DB's CHECK constraints on purpose. Validating here
turns a bad value into a 422 with a field-level message, instead of letting
asyncpg raise a constraint violation that surfaces as an opaque 500.
"""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

# MCP3008 is a 10-bit ADC.
ADC_MAX = 1023


def out(model: type[BaseModel], row) -> BaseModel:
    """Build a response model from an asyncpg Record.

    Record implements the mapping protocol but isn't a dict, so convert
    explicitly rather than relying on Pydantic's coercion.
    """
    return model.model_validate(dict(row))


# --- Auth --------------------------------------------------------------------

class SessionOut(BaseModel):
    token: str
    expires_in: int


# --- Plants ------------------------------------------------------------------

class PlantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    name_en: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=100)


class PlantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    name_en: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=100)


class PlantOut(BaseModel):
    id: int
    name: str
    name_en: str | None = None
    location: str | None = None
    created_at: datetime | None = None
    # Null when the plant has no watering_config row — i.e. no relay is
    # plumbed to it yet.
    mode: str | None = None
    auto_threshold: float | None = None
    last_watered_at: datetime | None = None


class SensorReadingOut(BaseModel):
    sensor_id: int
    plant_id: int | None = None
    address: str
    label: str | None = None
    sensor_type: str
    unit: str
    # Null when the sensor exists but has never reported.
    value: float | None = None
    raw_adc: int | None = None
    recorded_at: datetime | None = None


class PlantDetailOut(PlantOut):
    sensors: list[SensorReadingOut]


# --- Readings ----------------------------------------------------------------

class HistoryPointOut(BaseModel):
    recorded_at: datetime
    value: float
    raw_adc: int | None = None
    sensor_id: int
    address: str
    sensor_type: str


class AggregatePointOut(BaseModel):
    bucket: datetime
    avg_value: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    # readings_daily doesn't carry a sample count; readings_hourly does.
    sample_count: int | None = None
    sensor_id: int
    address: str
    sensor_type: str


# --- Watering ----------------------------------------------------------------

class WateringConfigPut(BaseModel):
    """Full create-or-replace of a plant's watering setup.

    Not in the plan's endpoint list, which only had PATCH — but PATCH can't
    create the first row, and nothing seeds watering_config (no relays are
    wired). Mirrors the PUT /sensors/{id}/calibration idiom already in the plan.
    """
    device_id: UUID
    mode: Literal["auto", "manual"] = "manual"
    auto_threshold: float = Field(default=30.0, ge=0, le=100)
    duration_sec: int = Field(default=10, gt=0, le=120)
    relay_channel: int = Field(ge=1, le=8)
    min_interval_min: int = Field(default=240, gt=0)


class WateringConfigPatch(BaseModel):
    mode: Literal["auto", "manual"] | None = None
    auto_threshold: float | None = Field(default=None, ge=0, le=100)
    duration_sec: int | None = Field(default=None, gt=0, le=120)
    min_interval_min: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def at_least_one_field(self):
        if not any(v is not None for v in self.__dict__.values()):
            raise ValueError("at least one field must be provided")
        return self


class WateringConfigOut(BaseModel):
    plant_id: int
    device_id: UUID
    mode: str
    auto_threshold: float
    duration_sec: int
    relay_channel: int
    min_interval_min: int
    updated_at: datetime | None = None


class CommandOut(BaseModel):
    command_id: UUID
    plant_id: int
    device_id: UUID
    triggered_by: str
    duration_sec: int
    status: str
    requested_at: datetime
    expires_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class CommandCreatedOut(BaseModel):
    command_id: UUID
    status: str


# --- Sensors -----------------------------------------------------------------

class SensorTypeOut(BaseModel):
    id: int
    code: str
    unit: str
    label: str | None = None


class AlertConfigPatch(BaseModel):
    alert_type: Literal["too_low", "too_high"]
    threshold: float
    cooldown_min: int = Field(default=60, gt=0)


class AlertConfigOut(BaseModel):
    id: int
    sensor_id: int
    alert_type: str
    threshold: float
    cooldown_min: int


class CalibrationPut(BaseModel):
    raw_dry: int = Field(ge=0, le=ADC_MAX)
    raw_wet: int = Field(ge=0, le=ADC_MAX)
    notes: str | None = None

    @model_validator(mode="after")
    def distinct_endpoints(self):
        # Mirrors sensor_calibration_distinct. Equal endpoints would be a
        # divide-by-zero in the Consumer's linear map.
        if self.raw_dry == self.raw_wet:
            raise ValueError("raw_dry and raw_wet must differ")
        return self


class CalibrationOut(BaseModel):
    sensor_id: int
    raw_dry: int
    raw_wet: int
    calibrated_at: datetime
    notes: str | None = None


# --- Alerts ------------------------------------------------------------------

class AlertOut(BaseModel):
    id: int
    plant_id: int | None = None
    sensor_id: int | None = None
    sensor_label: str | None = None
    alert_type: str
    value: float | None = None
    sent_at: datetime | None = None
    acknowledged: bool
