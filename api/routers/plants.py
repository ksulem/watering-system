from fastapi import APIRouter, Depends, HTTPException, status

from api import db
from api.dependencies import require_session
from api.schemas import (
    PlantCreate,
    PlantDetailOut,
    PlantOut,
    PlantUpdate,
    SensorReadingOut,
    out,
)

router = APIRouter(prefix="/plants", tags=["plants"],
                   dependencies=[Depends(require_session)])


@router.get("", response_model=list[PlantOut])
async def list_plants():
    return [out(PlantOut, r) for r in await db.list_plants()]


@router.get("/{plant_id}", response_model=PlantDetailOut)
async def get_plant(plant_id: int):
    plant = await db.get_plant(plant_id)
    if plant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plant not found.")
    sensors = await db.latest_readings(plant_id)
    return PlantDetailOut(**dict(plant),
                          sensors=[out(SensorReadingOut, s) for s in sensors])


@router.post("", response_model=PlantOut, status_code=status.HTTP_201_CREATED)
async def create_plant(body: PlantCreate):
    row = await db.create_plant(body.name, body.name_en, body.location)
    return out(PlantOut, row)


@router.patch("/{plant_id}", response_model=PlantOut)
async def update_plant(plant_id: int, body: PlantUpdate):
    row = await db.update_plant(plant_id, name=body.name, name_en=body.name_en,
                                location=body.location)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plant not found.")
    return out(PlantOut, row)
