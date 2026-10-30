"""
Status statistics API router.
Provides aggregated target counts grouped by status.
"""

from typing import Dict
from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import PostTarget, TargetStatus

router = APIRouter(prefix="/stats", tags=["System Statistics"])


@router.get("", response_model=Dict[str, int])
def get_target_stats(db: Session = Depends(get_db)):
    """
    Returns counts of scheduled targets broken down by status:
    - pending
    - processing
    - posted
    - failed
    - total
    """
    # Count grouped by status
    stmt = select(PostTarget.status, func.count(PostTarget.id)).group_by(PostTarget.status)
    rows = db.execute(stmt).all()

    # Initialize all known statuses with 0
    counts: Dict[str, int] = {status.value: 0 for status in TargetStatus}

    total = 0
    for status_val, count in rows:
        key = status_val.value if hasattr(status_val, "value") else str(status_val)
        counts[key] = count
        total += count

    counts["total"] = total
    return counts

