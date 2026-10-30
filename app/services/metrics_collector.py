"""
Metrics collector service for Threads.
Periodically queries and stores engagement snapshots at defined post ages (1h, 6h, 24h, 72h, 7d)
and collects daily account audience metrics.
"""

from datetime import datetime, timezone, timedelta
import json
import logging
import time
from typing import Optional, List, Tuple
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models.target import PostTarget, TargetStatus
from app.models.metric import MetricSnapshot, AccountSnapshot
from app.models.account import Account
from app.models.log import Log
from app.adapters.threads import ThreadsAdapter

logger = logging.getLogger(__name__)

# Standard age buckets for capturing lifecycle trajectory
AGE_BUCKETS: List[Tuple[str, timedelta]] = [
    ("1h", timedelta(hours=1)),
    ("6h", timedelta(hours=6)),
    ("24h", timedelta(hours=24)),
    ("72h", timedelta(hours=72)),
    ("7d", timedelta(days=7)),
]

MAX_CALLS_PER_RUN = 50
INTER_CALL_DELAY = 0.3


def extract_part_media_ids(target: PostTarget) -> List[Tuple[int, str]]:
    """
    Extracts (part_index, media_id) pairs from target.publish_state or external_post_id.
    """
    if target.publish_state:
        try:
            state = json.loads(target.publish_state)
            part_ids = state.get("published_part_ids", [])
            if part_ids and isinstance(part_ids, list):
                return [(idx, str(mid)) for idx, mid in enumerate(part_ids)]
        except Exception:
            pass

    if target.external_post_id:
        return [(0, str(target.external_post_id))]

    return []


def collect_due_snapshots(db: Session, adapter: Optional[ThreadsAdapter] = None) -> int:
    """
    Finds posted Threads targets from the last 14 days and collects any due
    age bucket snapshots (1h, 6h, 24h, 72h, 7d).
    Enforces a budget of at most 50 API calls per execution.
    Logs errors without failing other targets.
    Returns the number of saved snapshots.
    """
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(days=14)

    # 1. Query posted Threads targets within lookback window
    stmt = (
        select(PostTarget)
        .where(
            PostTarget.platform == "threads",
            PostTarget.status == TargetStatus.posted,
            PostTarget.posted_at.is_not(None),
            PostTarget.posted_at >= cutoff,
        )
        .order_by(PostTarget.posted_at.asc())
    )
    targets = db.scalars(stmt).all()

    if not targets:
        return 0

    threads_adapter = adapter or ThreadsAdapter()
    saved_snapshots = 0
    api_calls_made = 0

    for target in targets:
        if api_calls_made >= MAX_CALLS_PER_RUN:
            logger.info(f"Reached max API call limit ({MAX_CALLS_PER_RUN}) for this metrics collector run.")
            break

        posted_time = target.posted_at
        if posted_time.tzinfo is None:
            posted_time = posted_time.replace(tzinfo=timezone.utc)

        media_parts = extract_part_media_ids(target)
        if not media_parts:
            continue

        for part_idx, media_id in media_parts:
            if api_calls_made >= MAX_CALLS_PER_RUN:
                break

            for age_label, delta in AGE_BUCKETS:
                if api_calls_made >= MAX_CALLS_PER_RUN:
                    break

                # Post must be at least as old as the bucket threshold
                if now_utc < posted_time + delta:
                    continue

                # Check if this snapshot was already collected
                exists_stmt = select(MetricSnapshot.id).where(
                    MetricSnapshot.post_target_id == target.id,
                    MetricSnapshot.part_index == part_idx,
                    MetricSnapshot.age_label == age_label,
                )
                if db.scalars(exists_stmt).first() is not None:
                    continue

                # Pause briefly between API requests to respect Meta rate limits
                time.sleep(INTER_CALL_DELAY)
                api_calls_made += 1

                try:
                    insights = threads_adapter.get_media_insights(media_id, db)

                    snapshot = MetricSnapshot(
                        post_target_id=target.id,
                        platform="threads",
                        part_index=part_idx,
                        external_media_id=media_id,
                        age_label=age_label,
                        captured_at=now_utc,
                        views=insights.get("views"),
                        likes=insights.get("likes"),
                        replies=insights.get("replies"),
                        reposts=insights.get("reposts"),
                        quotes=insights.get("quotes"),
                        shares=insights.get("shares"),
                        raw_json=insights.get("raw_json", "{}"),
                    )
                    db.add(snapshot)
                    db.commit()
                    saved_snapshots += 1
                    logger.debug(f"Saved snapshot {age_label} for target #{target.id} part {part_idx}.")

                except Exception as exc:
                    db.rollback()
                    err_msg = str(exc)
                    # Record failure in logs table for debugging without crashing other posts
                    db.add(Log(
                        post_target_id=target.id,
                        level="ERROR",
                        message=f"Metrics collection failed for part {part_idx} ({age_label}): {err_msg}",
                    ))
                    db.commit()
                    logger.warning(f"Target #{target.id} part {part_idx} metrics error: {err_msg}")
                    break  # Skip remaining older buckets for this part if currently failing

    return saved_snapshots


def collect_account_snapshot(db: Session, adapter: Optional[ThreadsAdapter] = None) -> Optional[AccountSnapshot]:
    """
    Collects daily profile and audience metrics for the Threads account.
    Stores at most one snapshot per account per calendar date.
    """
    now_utc = datetime.now(timezone.utc)
    today_date = now_utc.date()

    # Find the Threads account row
    stmt = select(Account).where(Account.platform == "threads")
    account = db.scalars(stmt).first()
    if not account:
        logger.info("No Threads account configured. Skipping daily account metrics collection.")
        return None

    # Check if a snapshot for today already exists
    exists_stmt = select(AccountSnapshot).where(
        AccountSnapshot.account_id == account.id,
        AccountSnapshot.snapshot_date == today_date,
    )
    existing = db.scalars(exists_stmt).first()
    if existing:
        return existing

    threads_adapter = adapter or ThreadsAdapter()
    try:
        data = threads_adapter.get_account_insights(db)

        snapshot = AccountSnapshot(
            account_id=account.id,
            snapshot_date=today_date,
            followers_count=data.get("followers_count"),
            views=data.get("views"),
            likes=data.get("likes"),
            replies=data.get("replies"),
            reposts=data.get("reposts"),
            quotes=data.get("quotes"),
            clicks=data.get("clicks"),
            captured_at=now_utc,
        )
        db.add(snapshot)
        db.commit()
        db.refresh(snapshot)
        logger.info(f"Saved daily account snapshot for date {today_date}.")
        return snapshot

    except Exception as exc:
        db.rollback()
        logger.warning(f"Daily account metrics collection notice: {exc}")
        return None

