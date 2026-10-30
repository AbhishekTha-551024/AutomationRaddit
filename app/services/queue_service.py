"""
Queue control service for social media post dispatch.
Manages global publish pausing/resuming, overdue staggering, and startup overdue safeguards.
"""

from datetime import datetime, timezone, timedelta
import logging
from typing import Optional, Dict, Any, List
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from app.config import settings
from app.models.setting import AppSetting
from app.models.target import PostTarget, TargetStatus
from app.models.log import Log

logger = logging.getLogger(__name__)


def is_publishing_paused(db: Session) -> bool:
    """Returns True if dispatching is globally paused, False otherwise."""
    setting = db.get(AppSetting, "publishing_paused")
    if not setting:
        return False
    return str(setting.value).lower().strip() == "true"


def pause_publishing(db: Session) -> None:
    """Sets global publishing state to paused."""
    setting = db.get(AppSetting, "publishing_paused")
    now = datetime.now(timezone.utc)
    if not setting:
        setting = AppSetting(key="publishing_paused", value="true", updated_at=now)
        db.add(setting)
    else:
        setting.value = "true"
        setting.updated_at = now
    db.commit()
    logger.info("Publishing globally paused.")


def resume_publishing(db: Session) -> int:
    """
    Resumes global dispatch.
    Implements rate safeguard: spreads overdue pending targets by at least 60s apart per platform
    so the backlog doesn't fire simultaneously and risk platform rate limits.
    Returns the count of staggered overdue targets.
    """
    setting = db.get(AppSetting, "publishing_paused")
    now_utc = datetime.now(timezone.utc)
    if not setting:
        setting = AppSetting(key="publishing_paused", value="false", updated_at=now_utc)
        db.add(setting)
    else:
        setting.value = "false"
        setting.updated_at = now_utc
    db.commit()

    # Query pending overdue targets
    stmt = (
        select(PostTarget)
        .where(
            PostTarget.status == TargetStatus.pending,
            PostTarget.deleted_at.is_(None),
            PostTarget.scheduled_at <= now_utc,
        )
        .order_by(PostTarget.platform.asc(), PostTarget.scheduled_at.asc())
    )
    overdue_targets = db.scalars(stmt).all()

    # Group by platform
    by_platform: Dict[str, List[PostTarget]] = {}
    for t in overdue_targets:
        by_platform.setdefault(t.platform, []).append(t)

    staggered_count = 0
    for platform, targets in by_platform.items():
        for idx, t in enumerate(targets):
            new_time = now_utc + timedelta(seconds=idx * 60)
            t.scheduled_at = new_time
            t.next_retry_at = None
            db.add(Log(
                post_target_id=t.id,
                level="INFO",
                message=f"Resume queue safeguard: staggered dispatch to {new_time.isoformat()} (+{idx*60}s).",
            ))
            staggered_count += 1

    db.commit()
    logger.info(f"Publishing resumed. Staggered {staggered_count} overdue target(s).")
    return staggered_count


def is_overdue(scheduled_at: datetime, grace_hours: Optional[int] = None) -> bool:
    """
    Checks if a scheduled time has passed beyond the grace threshold (default 6 hours).
    Overdue posts require manual confirmation before dispatching.
    """
    hours = grace_hours if grace_hours is not None else settings.OVERDUE_GRACE_HOURS
    now_utc = datetime.now(timezone.utc)
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
    return scheduled_at < now_utc - timedelta(hours=hours)

