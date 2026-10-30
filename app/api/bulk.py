"""
Bulk Operations API router.
Supports batch operations across selected target IDs:
delete, retry, cancel, restore, and shift_schedule.
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app.database import get_db
from app.services.post_operations import execute_bulk_action

router = APIRouter(prefix="/bulk", tags=["Bulk Operations"])


class BulkActionRequest(BaseModel):
    action: str = Field(..., description="Action: 'delete', 'retry', 'cancel', 'restore', 'shift_schedule'")
    target_ids: List[int] = Field(..., min_items=1, description="List of target IDs to process")
    shift_hours: Optional[int] = Field(0, description="Hours to shift schedule if action is 'shift_schedule'")
    also_delete_platform: Optional[bool] = Field(False, description="Attempt remote platform deletion for posted items")


@router.post("")
def handle_bulk_action(payload: BulkActionRequest, db: Session = Depends(get_db)):
    """
    Executes a bulk operation across multiple target IDs.
    Returns per-item results indicating completed or skipped targets with human-readable reasons.
    """
    valid_actions = {"delete", "retry", "cancel", "restore", "shift_schedule"}
    if payload.action not in valid_actions:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid action '{payload.action}'. Supported actions: {sorted(list(valid_actions))}.",
        )

    res = execute_bulk_action(
        db=db,
        action=payload.action,
        target_ids=payload.target_ids,
        shift_hours=payload.shift_hours or 0,
        also_delete_platform=payload.also_delete_platform or False,
    )
    return res

