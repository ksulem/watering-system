from fastapi import APIRouter, Depends, HTTPException, status

from api import db
from api.dependencies import require_session
from api.schemas import (
    AlertConfigOut,
    AlertConfigPatch,
    CalibrationOut,
    CalibrationPut,
    SensorReadingOut,
    SensorTypeOut,
    out,
)

router = APIRouter(prefix="/sensors", tags=["sensors"],
                   dependencies=[Depends(require_session)])


async def _require_sensor(sensor_id: int) -> None:
    if not await db.sensor_exists(sensor_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sensor not found.")


# Declared before the /{sensor_id}/... routes so a literal path segment is never
# shadowed by a path parameter.
@router.get("/types", response_model=list[SensorTypeOut])
async def list_sensor_types():
    return [out(SensorTypeOut, r) for r in await db.list_sensor_types()]


@router.get("", response_model=list[SensorReadingOut])
async def list_sensors():
    """All sensors with their plant assignment and most recent reading."""
    return [out(SensorReadingOut, r) for r in await db.latest_readings()]


@router.get("/{sensor_id}/alert-config", response_model=list[AlertConfigOut])
async def get_alert_config(sensor_id: int):
    await _require_sensor(sensor_id)
    return [out(AlertConfigOut, r) for r in await db.get_alert_configs(sensor_id)]


@router.patch("/{sensor_id}/alert-config", response_model=AlertConfigOut)
async def set_alert_config(sensor_id: int, body: AlertConfigPatch):
    """Set one direction's threshold.

    A sensor can have both a too_low and a too_high rule (unique per
    sensor+alert_type), so this upserts the one named in the body and leaves
    the other alone.
    """
    await _require_sensor(sensor_id)
    row = await db.upsert_alert_config(sensor_id, body.alert_type, body.threshold,
                                       body.cooldown_min)
    return out(AlertConfigOut, row)


@router.get("/{sensor_id}/calibration", response_model=CalibrationOut)
async def get_calibration(sensor_id: int):
    await _require_sensor(sensor_id)
    row = await db.get_calibration(sensor_id)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No calibration row — the Consumer is using sensor-type defaults.")
    return out(CalibrationOut, row)


@router.put("/{sensor_id}/calibration", response_model=CalibrationOut)
async def put_calibration(sensor_id: int, body: CalibrationPut):
    """Recalibrate a sensor.

    Takes effect on the next reading: the Consumer reads this table per reading,
    so nothing has to be redeployed to the Pi. Past readings keep the value they
    were stored with — raw_adc is preserved alongside, so they can be
    recomputed later if it matters.
    """
    await _require_sensor(sensor_id)
    row = await db.put_calibration(sensor_id, body.raw_dry, body.raw_wet, body.notes)
    return out(CalibrationOut, row)
