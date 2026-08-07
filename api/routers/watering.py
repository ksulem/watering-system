from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status

from api import db
from api.dependencies import require_session
from api.schemas import (
    CommandCreatedOut,
    CommandOut,
    WateringConfigOut,
    WateringConfigPatch,
    WateringConfigPut,
    out,
)

router = APIRouter(tags=["watering"], dependencies=[Depends(require_session)])

# Same TTL the Consumer uses for auto-water. The Pi refuses to act on a command
# past expires_at, so this bounds how stale a queued watering can get if the Pi
# is offline when it's issued.
COMMAND_TTL = timedelta(minutes=5)


@router.get("/plants/{plant_id}/watering/config", response_model=WateringConfigOut)
async def get_config(plant_id: int):
    row = await db.get_watering_config(plant_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "No watering config for this plant.")
    return out(WateringConfigOut, row)


@router.put("/plants/{plant_id}/watering/config", response_model=WateringConfigOut)
async def put_config(plant_id: int, body: WateringConfigPut):
    """Create or replace a plant's watering setup.

    PUT rather than POST because plant_id is the key — there's at most one
    config per plant. This is the only way to create the first row; PATCH can
    only amend an existing one.
    """
    if not await db.plant_exists(plant_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plant not found.")
    try:
        row = await db.put_watering_config(
            plant_id=plant_id, device_id=body.device_id, mode=body.mode,
            auto_threshold=body.auto_threshold, duration_sec=body.duration_sec,
            relay_channel=body.relay_channel, min_interval_min=body.min_interval_min)
    except Exception as e:
        # watering_relay_unique_per_device: one relay drives one solenoid, so
        # two plants on the same (device, channel) would water the wrong plant.
        if "watering_relay_unique_per_device" in str(e):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Relay channel {body.relay_channel} on that device is already "
                "assigned to another plant.") from e
        if "watering_config_device_id_fkey" in str(e):
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "Unknown device_id.") from e
        raise
    return out(WateringConfigOut, row)


@router.patch("/plants/{plant_id}/watering/config", response_model=WateringConfigOut)
async def patch_config(plant_id: int, body: WateringConfigPatch):
    row = await db.patch_watering_config(
        plant_id, mode=body.mode, auto_threshold=body.auto_threshold,
        duration_sec=body.duration_sec, min_interval_min=body.min_interval_min)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "No watering config for this plant — use PUT to create one.")
    return out(WateringConfigOut, row)


@router.post("/plants/{plant_id}/water", response_model=CommandCreatedOut,
             status_code=status.HTTP_202_ACCEPTED)
async def water_now(plant_id: int):
    """Queue a manual watering.

    202, not 201: this only writes an outbox row. The Dispatcher publishes it,
    and the Pi decides whether the relay actually fires. Poll
    GET /commands/{command_id}/status for the real outcome.
    """
    config = await db.get_watering_config(plant_id)
    if config is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No watering config for this plant — no relay is assigned to it.")
    if config["mode"] != "manual":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Plant is in auto mode; switch to manual before watering by hand.")

    command_id = uuid4()
    created = await db.try_create_pending_watering_command(
        plant_id=plant_id,
        device_id=config["device_id"],
        command_id=command_id,
        triggered_by="manual",
        duration_sec=config["duration_sec"],
        relay_channel=config["relay_channel"],
        expires_at=datetime.now(UTC) + COMMAND_TTL,
    )
    if not created:
        # Lost the plant-row lock race, or a command is already in flight —
        # either way this plant already has a watering pending.
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "A watering command is already in flight for this plant.")
    return CommandCreatedOut(command_id=command_id, status="pending")


@router.get("/commands/{command_id}/status", response_model=CommandOut)
async def command_status(command_id: UUID):
    row = await db.get_command(command_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Command not found.")
    return out(CommandOut, row)
