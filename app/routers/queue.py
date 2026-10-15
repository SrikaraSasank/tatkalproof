from fastapi import APIRouter, Depends

from ..auth import CurrentUser, get_current_user
from ..flags import get_flags
from ..redis_client import get_redis
from ..schemas import QueueStatusOut
from ..waiting_room import join_queue, queue_status

router = APIRouter(prefix="/queue", tags=["waiting room"])


@router.post("/{event_id}/join", response_model=QueueStatusOut)
async def join(event_id: int, user: CurrentUser = Depends(get_current_user)) -> QueueStatusOut:
    r = get_redis()
    enabled = (await get_flags(r)).waiting_room_enabled
    if not enabled:
        return QueueStatusOut(event_id=event_id, admitted=True, waiting_room_enabled=False)
    position = await join_queue(r, event_id, user.id)
    admitted, pos, ttl = await queue_status(r, event_id, user.id)
    return QueueStatusOut(
        event_id=event_id, admitted=admitted, position=pos if position is not None else None,
        pass_expires_in=ttl, waiting_room_enabled=True,
    )


@router.get("/{event_id}/status", response_model=QueueStatusOut)
async def status(event_id: int, user: CurrentUser = Depends(get_current_user)) -> QueueStatusOut:
    r = get_redis()
    enabled = (await get_flags(r)).waiting_room_enabled
    if not enabled:
        return QueueStatusOut(event_id=event_id, admitted=True, waiting_room_enabled=False)
    admitted, position, ttl = await queue_status(r, event_id, user.id)
    return QueueStatusOut(
        event_id=event_id, admitted=admitted, position=position, pass_expires_in=ttl, waiting_room_enabled=True
    )
