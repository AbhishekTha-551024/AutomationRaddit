"""
System audit logs API router.
Provides query access to recent execution logs across all targets.
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Log
from app.api.targets import LogResponse
from app.api.posts import format_to_ist

router = APIRouter(prefix="/logs", tags=["Audit Logs"])


@router.get("", response_model=List[LogResponse])
def list_logs(
    level: Optional[str] = Query(None, description="Optional log level filter: INFO, WARNING, ERROR"),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of logs to return"),
    db: Session = Depends(get_db),
):
    """
    Retrieve latest system logs sorted newest first.
    Optionally filter by severity level (INFO, WARNING, ERROR).
    """
    stmt = select(Log)

    if level:
        stmt = stmt.where(Log.level == level.strip().upper())

    stmt = stmt.order_by(Log.created_at.desc()).limit(limit)
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

