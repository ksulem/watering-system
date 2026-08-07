from fastapi import APIRouter, Depends, HTTPException, Query, status

from api import db
from api.dependencies import require_session
from api.schemas import AlertOut, out

router = APIRouter(prefix="/alerts", tags=["alerts"],
                   dependencies=[Depends(require_session)])


@router.get("", response_model=list[AlertOut])
async def list_alerts(
    unacked: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
):
    return [out(AlertOut, r) for r in await db.list_alerts(unacked=unacked, limit=limit)]


@router.post("/{alert_id}/ack", response_model=AlertOut)
async def ack_alert(alert_id: int):
    row = await db.ack_alert(alert_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Alert not found.")
    return out(AlertOut, row)
