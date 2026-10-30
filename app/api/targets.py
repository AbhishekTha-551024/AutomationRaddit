"""
Target platforms API router.
Provides endpoints to manage target lifecycles:
view logs, retry failed dispatches, reschedule, post now, cancel, soft delete, and restore.
"""

from datetime import datetime, timezone
from typing import List, Optional, Union
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import PostTarget, Log, TargetStatus
from app.api.posts import format_to_ist, parse_to_utc, TargetResponse
from app.services.post_operations import (
    soft_delete_target,
    restore_target,
    reschedule_target,
    post_now_target,
    cancel_target,
)

router = APIRouter(prefix="/targets", tags=["Targets & Logs"])


class LogResponse(BaseModel):
    """Log record formatted for presentation."""
    id: int
    post_target_id: int
    level: str
    message: str
    created_at_ist: str


class ScheduleChangeRequest(BaseModel):
    """Payload to reschedule a target."""
    scheduled_at: Union[datetime, str] = Field(
        ...,
        description="New scheduled ISO datetime (naive times assume IST).",
    )


def build_target_response(target: PostTarget) -> TargetResponse:
    return TargetResponse(
        id=target.id,
        platform=target.platform,
        status=target.status.value,
        scheduled_at_ist=format_to_ist(target.scheduled_at) or "",
        next_retry_at_ist=format_to_ist(target.next_retry_at),
        attempts=target.attempts,
        external_post_id=target.external_post_id,
        error_message=target.error_message,
        publish_state=target.publish_state,
        deleted_at_ist=format_to_ist(target.deleted_at),
    )


@router.get("/{target_id}/logs", response_model=List[LogResponse])
def get_target_logs(target_id: int, db: Session = Depends(get_db)):
    """
    Retrieve all audit and execution logs for a specific post target.
    Sorted chronologically from oldest to newest.
    """
    target = db.get(PostTarget, target_id)
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PostTarget #{target_id} not found",
        )

    stmt = (
        select(Log)
        .where(Log.post_target_id == target_id)
        .order_by(Log.created_at.asc())
    )
    logs = db.scalars(stmt).all()

    return [
        LogResponse(
            id=log_entry.id,
            post_target_id=log_entry.post_target_id,
            level=log_entry.level,
            message=log_entry.message,
            created_at_ist=format_to_ist(log_entry.created_at) or "",
        )
        for log_entry in logs
    ]


@router.post("/{target_id}/retry", response_model=TargetResponse)
def retry_failed_target(target_id: int, db: Session = Depends(get_db)):
    """
    Manually reset a failed target back to 'pending' for immediate dispatch.
    Only permitted if the target is currently in 'failed' status.
    """
    target = db.get(PostTarget, target_id)
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"PostTarget #{target_id} not found",
        )

    if target.status != TargetStatus.failed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot retry target #{target_id}. Only targets with status='failed' can be retried (current status: '{target.status.value}').",
        )

    # Reset state to pending for immediate re-attempt
    now_utc = datetime.now(timezone.utc)
    target.status = TargetStatus.pending
    target.attempts = 0
    target.next_retry_at = None
    target.scheduled_at = now_utc
    target.error_message = None

    db.add(Log(
        post_target_id=target_id,
        level="INFO",
        message="Manual retry triggered. Target status reset to 'pending' with attempts=0.",
    ))
    db.commit()
    db.refresh(target)

    return build_target_response(target)


@router.patch("/{target_id}/schedule", response_model=TargetResponse)
def reschedule_post_target(
    target_id: int,
    payload: ScheduleChangeRequest,
    db: Session = Depends(get_db),
):
    """
    Reschedule a target in pending, failed, or cancelled status to a new time.
    Resets attempts to 0 and clears next_retry_at.
    """
    new_utc = parse_to_utc(payload.scheduled_at)
    try:
        reschedule_target(db, target_id=target_id, new_scheduled_at=new_utc)
    except ValueError as ve:
        err_msg = str(ve)
        if "not found" in err_msg.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=err_msg)
        elif "past" in err_msg.lower():
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=err_msg)
        else:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=err_msg)

    target = db.get(PostTarget, target_id)
    return build_target_response(target)


@router.post("/{target_id}/post-now", response_model=TargetResponse)
def post_target_now(target_id: int, db: Session = Depends(get_db)):
    """
    Immediately schedule a pending, failed, or cancelled target for instant dispatch.
    """
    try:
        post_now_target(db, target_id=target_id)
    except ValueError as ve:
        err_msg = str(ve)
        if "not found" in err_msg.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=err_msg)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=err_msg)

    target = db.get(PostTarget, target_id)
    return build_target_response(target)


@router.post("/{target_id}/cancel", response_model=TargetResponse)
def cancel_pending_target(target_id: int, db: Session = Depends(get_db)):
    """
    Cancels a pending target so the scheduler will not publish it.
    """
    try:
        cancel_target(db, target_id=target_id)
    except ValueError as ve:
        err_msg = str(ve)
        if "not found" in err_msg.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=err_msg)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=err_msg)

    target = db.get(PostTarget, target_id)
    return build_target_response(target)


@router.delete("/{target_id}")
def delete_single_target(
    target_id: int,
    also_delete_platform: bool = Query(False),
    db: Session = Depends(get_db),
):
    """
    Soft-deletes a single target platform.
    Blocked with 409 if currently sending.
    """
    try:
        res = soft_delete_target(db, target_id=target_id, also_delete_platform=also_delete_platform)
        return res
    except ValueError as ve:
        err_msg = str(ve)
        if "not found" in err_msg.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=err_msg)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=err_msg)


@router.post("/{target_id}/restore", response_model=TargetResponse)
def restore_single_target(target_id: int, db: Session = Depends(get_db)):
    """
    Restores a soft-deleted target from Trash.
    """
    try:
        restore_target(db, target_id=target_id)
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(ve))

    target = db.get(PostTarget, target_id)
    return build_target_response(target)
