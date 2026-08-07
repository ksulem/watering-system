from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status

from api import config, db
from api.dependencies import require_session
from api.schemas import AggregatePointOut, HistoryPointOut, SensorReadingOut, out

router = APIRouter(prefix="/plants", tags=["readings"],
                   dependencies=[Depends(require_session)])

DEFAULT_WINDOW = timedelta(hours=24)


def _window(frm: datetime | None, to: datetime | None) -> tuple[datetime, datetime]:
    """Resolve an optional time range, defaulting to the last 24 hours."""
    to = to or datetime.now(UTC)
    frm = frm or (to - DEFAULT_WINDOW)
    if frm >= to:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "`from` must be before `to`.")
    return frm, to


async def _require_plant(plant_id: int) -> None:
    if not await db.plant_exists(plant_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plant not found.")


@router.get("/{plant_id}/readings/latest", response_model=list[SensorReadingOut])
async def latest_readings(plant_id: int):
    await _require_plant(plant_id)
    return [out(SensorReadingOut, r) for r in await db.latest_readings(plant_id)]


@router.get("/{plant_id}/readings/history", response_model=list[HistoryPointOut])
async def reading_history(
    plant_id: int,
    sensor_type: str | None = None,
    frm: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    limit: int = Query(default=config.DEFAULT_HISTORY_ROWS, ge=1,
                       le=config.MAX_HISTORY_ROWS),
):
    """Raw readings, newest first.

    `limit` is capped: at a 60-second cadence a year of one sensor is ~500k
    rows, and nothing good comes of serializing that into a single response.
    Use the aggregated endpoint for anything longer than a day or two.
    """
    await _require_plant(plant_id)
    frm, to = _window(frm, to)
    rows = await db.reading_history(plant_id, sensor_type, frm, to, limit)
    return [out(HistoryPointOut, r) for r in rows]


@router.get("/{plant_id}/readings/aggregated", response_model=list[AggregatePointOut])
async def reading_aggregated(
    plant_id: int,
    sensor_type: str | None = None,
    bucket: str = Query(default="1h", pattern="^(1h|1d)$"),
    frm: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    limit: int = Query(default=config.DEFAULT_HISTORY_ROWS, ge=1,
                       le=config.MAX_HISTORY_ROWS),
):
    """Pre-aggregated readings from the TimescaleDB continuous aggregates.

    `bucket` is pattern-constrained to the two materialized views that exist;
    it selects a fixed SQL string rather than being interpolated anywhere.
    """
    await _require_plant(plant_id)
    frm, to = _window(frm, to)
    rows = await db.reading_aggregated(plant_id, sensor_type, bucket, frm, to, limit)
    return [out(AggregatePointOut, r) for r in rows]
