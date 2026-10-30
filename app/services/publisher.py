"""
Publisher service that queries due posts, atomically claims them,
dispatches them to their platform adapters, and manages retries and logs.
"""

from datetime import datetime, timezone, timedelta
import logging
from sqlalchemy import select, update, or_, and_
from sqlalchemy.orm import Session
from app.config import settings
from app.models import PostTarget, Log, TargetStatus, Post
from app.adapters.registry import get_adapter
from app.api.posts import format_to_ist
from app.services.queue_service import is_publishing_paused

logger = logging.getLogger(__name__)


def trigger_failed_target_alert(target: PostTarget, db: Session) -> None:
    """Dispatches a failure alert to Telegram when a target fails permanently."""
    try:
        from app.services.alerts import send_alert
        from app.services.post_operations import friendly_error_message
        post = target.post
        post_title = (post.title or post.body[:40] or "Untitled") if post else "Post"
        if len(post_title) > 40:
            post_title = post_title[:37] + "..."
        friendly = friendly_error_message(target.error_message)
        title_text = f"Post failed on {target.platform.capitalize()}"
        body_text = (
            f"Post: {post_title}\n"
            f"Error: {friendly['title']} — {friendly['message']}\n"
            f"Suggested fix: {friendly['suggestion']}\n"
            f"Details: {settings.BASE_URL}/posts/{target.post_id}"
        )
        send_alert(
            kind="failed_post",
            title=title_text,
            body=body_text,
            dedupe_key=f"target_failed:{target.id}",
            db=db,
        )
    except Exception as exc:
        logger.warning(f"Notice: Failed to dispatch Telegram alert for failed target #{target.id}: {exc}")


def run_due_posts(db: Session) -> int:
    """
    Checks the database for post_targets due for publishing.
    1. Selects targets with status='pending' and:
       ((next_retry_at IS NULL AND scheduled_at <= now) OR next_retry_at <= now) (all UTC).
    2. Atomically claims each target by setting status='processing'.
    3. Calls the platform adapter.
    4. On success: sets status='posted' and clears next_retry_at.
    5. On failure:
       - If error is non-retryable (permanent): immediately marks 'failed'.
       - If attempts < 3: increments attempts, schedules next_retry_at, sets status='pending'.
       - If attempts >= 3: marks 'failed' and clears next_retry_at.
    6. Writes a detailed Log row for every step.
    7. One failing target never stops other targets.

    Returns the number of targets processed.
    """
    # Check if publishing is globally paused
    if is_publishing_paused(db):
        logger.debug("Publishing is globally paused. Skipping due post dispatch.")
        return 0

    now_utc = datetime.now(timezone.utc)
    overdue_cutoff = now_utc - timedelta(hours=settings.OVERDUE_GRACE_HOURS)

    # 1. Query pending targets due for initial publish or retry
    # Excludes soft-deleted targets, targets whose parent post is deleted,
    # and targets overdue by more than OVERDUE_GRACE_HOURS (which require user confirmation)
    due_filter = and_(
        PostTarget.status == TargetStatus.pending,
        PostTarget.deleted_at.is_(None),
        PostTarget.post.has(Post.deleted_at.is_(None)),
        PostTarget.scheduled_at >= overdue_cutoff,
        or_(
            and_(PostTarget.next_retry_at.is_(None), PostTarget.scheduled_at <= now_utc),
            and_(PostTarget.next_retry_at.is_not(None), PostTarget.next_retry_at <= now_utc),
        ),
    )
    stmt = select(PostTarget).where(due_filter)
    due_targets = db.scalars(stmt).all()

    if not due_targets:
        return 0

    logger.info(f"Found {len(due_targets)} due post target(s) for processing.")
    processed_count = 0
    delays = settings.retry_delays

    for target in due_targets:
        target_id = target.id

        # 2. Atomically claim target: status must still be 'pending' and not deleted
        # Ensures that if another process, thread, or user action touched it, we skip it
        claim_stmt = (
            update(PostTarget)
            .where(
                PostTarget.id == target_id,
                PostTarget.status == TargetStatus.pending,
                PostTarget.deleted_at.is_(None),
            )
            .values(status=TargetStatus.processing)
        )
        claim_result = db.execute(claim_stmt)
        db.commit()

        if claim_result.rowcount == 0:
            logger.info(f"Target #{target_id} already claimed. Skipping.")
            continue

        # Record 'claimed' log entry
        db.add(Log(
            post_target_id=target_id,
            level="INFO",
            message=f"Target #{target_id} ({target.platform}) claimed for dispatch.",
        ))
        db.commit()

        # 3. Dispatch to adapter with isolated error handling
        try:
            current_target = db.get(PostTarget, target_id)
            if not current_target or not current_target.post:
                error_msg = f"Target #{target_id} parent post is missing."
                if current_target:
                    current_target.status = TargetStatus.failed
                    current_target.error_message = error_msg
                    db.add(Log(post_target_id=target_id, level="ERROR", message=error_msg))
                    db.commit()
                continue

            adapter = get_adapter(current_target.platform)
            publish_result = adapter.publish(current_target.post, current_target)

            new_attempts = current_target.attempts + 1

            if publish_result.success:
                # --- SUCCESS ---
                current_target.status = TargetStatus.posted
                current_target.posted_at = now_utc
                current_target.external_post_id = publish_result.external_post_id
                current_target.error_message = None
                current_target.next_retry_at = None

                db.add(Log(
                    post_target_id=target_id,
                    level="INFO",
                    message=f"Successfully published to {current_target.platform}. External ID: {publish_result.external_post_id}",
                ))
                logger.info(f"Target #{target_id} posted successfully.")

                # Automatically extract and store post features for analysis (never fails publishing)
                try:
                    from app.services.tagging import tag_post
                    tag_post(db, current_target)
                except Exception as tag_exc:
                    logger.warning(f"Target #{target_id} auto-tagging notice: {tag_exc}")

            else:
                # --- FAILURE ---
                current_target.attempts = new_attempts
                current_target.error_message = publish_result.error_message

                # Case A: Permanent non-retryable error (e.g. 400 Bad Request, 401 Unauthorized, 403 Forbidden)
                if not publish_result.retryable:
                    current_target.status = TargetStatus.failed
                    current_target.next_retry_at = None

                    db.add(Log(
                        post_target_id=target_id,
                        level="ERROR",
                        message=f"Permanent error on {current_target.platform}: {publish_result.error_message}. Giving up without retries.",
                    ))
                    logger.warning(f"Target #{target_id} failed permanently: {publish_result.error_message}")
                    trigger_failed_target_alert(current_target, db)

                # Case B: Retryable error and attempts < 3
                elif new_attempts < 3:
                    # Pick delay in minutes for this attempt
                    delay_idx = min(new_attempts - 1, len(delays) - 1)
                    delay_minutes = delays[delay_idx]
                    next_retry = now_utc + timedelta(minutes=delay_minutes)

                    current_target.status = TargetStatus.pending
                    current_target.next_retry_at = next_retry

                    retry_ist_str = format_to_ist(next_retry)
                    db.add(Log(
                        post_target_id=target_id,
                        level="WARNING",
                        message=f"Attempt {new_attempts}/3 failed: {publish_result.error_message}. Retry #{new_attempts + 1} scheduled for {retry_ist_str} (in {delay_minutes}m).",
                    ))
                    logger.warning(f"Target #{target_id} attempt {new_attempts} failed. Next retry: {retry_ist_str}")

                # Case C: Retryable error but attempts >= 3 (max retries reached)
                else:
                    current_target.status = TargetStatus.failed
                    current_target.next_retry_at = None

                    db.add(Log(
                        post_target_id=target_id,
                        level="ERROR",
                        message=f"Attempt {new_attempts}/3 failed: {publish_result.error_message}. Maximum retries exceeded. Giving up.",
                    ))
                    logger.error(f"Target #{target_id} permanently failed after {new_attempts} attempts.")
                    trigger_failed_target_alert(current_target, db)

            db.commit()
            processed_count += 1

        except Exception as exc:
            db.rollback()
            err_text = str(exc)
            logger.error(f"Unexpected exception processing target #{target_id}: {err_text}")

            target_to_update = db.get(PostTarget, target_id)
            if target_to_update:
                new_attempts = target_to_update.attempts + 1
                target_to_update.attempts = new_attempts
                target_to_update.error_message = err_text

                if new_attempts < 3:
                    delay_minutes = delays[min(new_attempts - 1, len(delays) - 1)]
                    next_retry = now_utc + timedelta(minutes=delay_minutes)
                    target_to_update.status = TargetStatus.pending
                    target_to_update.next_retry_at = next_retry
                    db.add(Log(
                        post_target_id=target_id,
                        level="WARNING",
                        message=f"Attempt {new_attempts}/3 crashed: {err_text}. Retry scheduled in {delay_minutes}m.",
                    ))
                else:
                    target_to_update.status = TargetStatus.failed
                    target_to_update.next_retry_at = None
                    db.add(Log(
                        post_target_id=target_id,
                        level="ERROR",
                        message=f"Attempt {new_attempts}/3 crashed: {err_text}. Maximum retries exceeded.",
                    ))
                    trigger_failed_target_alert(target_to_update, db)
                db.commit()

            processed_count += 1

    return processed_count
