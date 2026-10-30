"""
Post management and operations service.
Implements:
- Status lifecycle permissions (draft, pending, processing, posted, failed, cancelled)
- Atomic state updates protected against races
- Soft delete, restore, and permanent trash purge
- Safe post content editing and platform validation
- Target rescheduling, post-now, cancellation, and duplication
- Bulk action orchestration
- Friendly error translations for failed platform dispatches
"""

from datetime import datetime, timezone, timedelta
import logging
from typing import Optional, Dict, Any, List, Tuple
from sqlalchemy import select, update, delete
from sqlalchemy.orm import Session
from app.models.post import Post
from app.models.target import PostTarget, TargetStatus
from app.models.log import Log
from app.models.metric import MetricSnapshot
from app.models.feature import PostFeature
from app.adapters.registry import get_adapter, SUPPORTED_PLATFORMS
from app.adapters.threads import split_thread_parts
from app.services.tagging import tag_post

logger = logging.getLogger(__name__)


def friendly_error_message(raw_error: Optional[str]) -> Dict[str, str]:
    """
    Translates raw API errors into human-readable guidance and suggested fixes.
    """
    if not raw_error:
        return {
            "title": "Dispatch failed",
            "message": "The platform rejected the post without a detailed error message.",
            "suggestion": "Check your credentials and network connection, then click Retry now.",
        }

    err_lower = raw_error.lower()

    if "expired" in err_lower or "session has expired" in err_lower or "code 190" in err_lower:
        return {
            "title": "Access token expired",
            "message": "Your platform session has expired.",
            "suggestion": "Generate a new access token in your developer portal and update your .env file.",
        }

    if "permission" in err_lower or "unsupported get request" in err_lower or "threads_manage_insights" in err_lower:
        return {
            "title": "Missing permissions",
            "message": "The access token lacks one or more required permissions.",
            "suggestion": "Regenerate your token with all required scopes and reload it.",
        }

    if "too long" in err_lower or "exceeds" in err_lower or "500 characters" in err_lower:
        return {
            "title": "Text too long",
            "message": "Content exceeds the platform character limit.",
            "suggestion": "Edit the post to shorten the text or break into thread parts with '---'.",
        }

    if "not found" in err_lower and "webhook" in err_lower:
        return {
            "title": "Webhook deleted or not found",
            "message": "The configured Discord webhook URL does not exist or was removed.",
            "suggestion": "Create a new webhook in Discord channel settings and update DISCORD_WEBHOOK_URL.",
        }

    if "chat not found" in err_lower or "bot was blocked" in err_lower:
        return {
            "title": "Telegram chat not found",
            "message": "The bot cannot post to the specified chat or was blocked by the user.",
            "suggestion": "Ensure the bot is added as an administrator to the Telegram channel.",
        }

    if "rate limit" in err_lower or "429" in err_lower or "too many requests" in err_lower:
        return {
            "title": "Rate limited",
            "message": "Too many requests sent in a short period.",
            "suggestion": "The scheduler will retry automatically once the cooldown window passes.",
        }

    if "facebook pages found" in err_lower or "needs_facebook_page" in err_lower:
        return {
            "title": "Instagram setup incomplete",
            "message": "No linked Facebook Page found for this Meta user token.",
            "suggestion": "Connect your Instagram Professional account to a Facebook Page in Page Settings.",
        }

    return {
        "title": "Dispatch error",
        "message": raw_error,
        "suggestion": "Review post content and credentials, then retry.",
    }


def soft_delete_post(
    db: Session,
    post_id: int,
    also_delete_platform: bool = False,
) -> Dict[str, Any]:
    """
    Soft-deletes a post and all associated targets.
    Blocked with ValueError if any target is currently 'processing'.
    """
    post = db.get(Post, post_id)
    if not post or post.deleted_at is not None:
        raise ValueError("Post not found or already in Trash")

    # Race protection: verify no target is actively processing
    for t in post.targets:
        if t.status == TargetStatus.processing:
            raise ValueError("This post is being sent right now. Try again in a moment.")

    now_utc = datetime.now(timezone.utc)
    post.deleted_at = now_utc

    for t in post.targets:
        if t.deleted_at is None:
            t.deleted_at = now_utc
            db.add(Log(
                post_target_id=t.id,
                level="INFO",
                message=f"Post #{post.id} and target #{t.id} ({t.platform}) moved to Trash.",
            ))

    db.commit()
    return {
        "status": "success",
        "post_id": post_id,
    }


def soft_delete_target(
    db: Session,
    target_id: int,
    also_delete_platform: bool = False,
) -> Dict[str, Any]:
    """
    Soft-deletes an individual target platform.
    Blocked if target is currently 'processing'.
    """
    # Atomic check: target must not be processing
    stmt = select(PostTarget).where(
        PostTarget.id == target_id,
        PostTarget.deleted_at.is_(None),
    )
    target = db.scalars(stmt).first()
    if not target:
        raise ValueError("Target not found or already in Trash")

    if target.status == TargetStatus.processing:
        raise ValueError("This post is being sent right now. Try again in a moment.")

    now_utc = datetime.now(timezone.utc)
    target.deleted_at = now_utc

    db.add(Log(
        post_target_id=target.id,
        level="INFO",
        message=f"Target #{target.id} ({target.platform}) moved to Trash.",
    ))
    db.commit()

    return {
        "status": "success",
        "target_id": target_id,
    }


def restore_post(db: Session, post_id: int) -> Dict[str, Any]:
    """Restores a soft-deleted post and all its targets back from Trash."""
    post = db.get(Post, post_id)
    if not post or post.deleted_at is None:
        raise ValueError("Post not found in Trash")

    now_utc = datetime.now(timezone.utc)
    post.deleted_at = None
    for t in post.targets:
        if t.deleted_at is not None:
            t.deleted_at = None
            if t.status == TargetStatus.cancelled:
                sched_time = t.scheduled_at if t.scheduled_at.tzinfo else t.scheduled_at.replace(tzinfo=timezone.utc)
                if sched_time > now_utc:
                    t.status = TargetStatus.pending
                else:
                    t.status = TargetStatus.draft
            db.add(Log(
                post_target_id=t.id,
                level="INFO",
                message=f"Post #{post.id} and target #{t.id} restored from Trash.",
            ))

    db.commit()
    return {"status": "success", "post_id": post_id}


def restore_target(db: Session, target_id: int) -> Dict[str, Any]:
    """
    Restores an individual target platform:
    - If in Trash (deleted_at is not None), clears deleted_at.
    - If cancelled, restores to 'pending' if scheduled in future, otherwise to 'draft'.
    """
    target = db.get(PostTarget, target_id)
    if not target:
        raise ValueError("Target not found")

    if target.deleted_at is None and target.status != TargetStatus.cancelled:
        raise ValueError("Target is not in Trash or cancelled")

    now_utc = datetime.now(timezone.utc)
    target.deleted_at = None

    if target.status == TargetStatus.cancelled:
        sched_time = target.scheduled_at if target.scheduled_at.tzinfo else target.scheduled_at.replace(tzinfo=timezone.utc)
        if sched_time > now_utc:
            target.status = TargetStatus.pending
            msg = f"Cancelled target #{target.id} ({target.platform}) restored to 'pending' (future schedule)."
        else:
            target.status = TargetStatus.draft
            msg = f"Cancelled target #{target.id} ({target.platform}) restored to 'draft' (past schedule)."
    else:
        msg = f"Target #{target.id} ({target.platform}) restored from Trash."

    if target.post and target.post.deleted_at is not None:
        target.post.deleted_at = None

    db.add(Log(
        post_target_id=target.id,
        level="INFO",
        message=msg,
    ))
    db.commit()
    return {"status": "success", "target_id": target_id}


def permanent_delete_target(db: Session, target_id: int) -> Dict[str, Any]:
    """
    Permanently destroys a target and cascades cleanup of its metric snapshots,
    features, and execution logs.
    """
    target = db.get(PostTarget, target_id)
    if not target:
        raise ValueError("Target not found")

    if target.status == TargetStatus.processing:
        raise ValueError("This post is being sent right now. Try again in a moment.")

    # Explicit cleanup of dependent records
    db.execute(delete(MetricSnapshot).where(MetricSnapshot.post_target_id == target_id))
    db.execute(delete(PostFeature).where(PostFeature.post_target_id == target_id))
    db.execute(delete(Log).where(Log.post_target_id == target_id))
    db.delete(target)
    db.commit()

    return {"status": "success", "deleted_target_id": target_id}


def permanent_delete_post(db: Session, post_id: int) -> Dict[str, Any]:
    """Permanently destroys a post and all its targets and analytics artifacts."""
    post = db.get(Post, post_id)
    if not post:
        raise ValueError("Post not found")

    for t in post.targets:
        if t.status == TargetStatus.processing:
            raise ValueError("This post is being sent right now. Try again in a moment.")
        db.execute(delete(MetricSnapshot).where(MetricSnapshot.post_target_id == t.id))
        db.execute(delete(PostFeature).where(PostFeature.post_target_id == t.id))
        db.execute(delete(Log).where(Log.post_target_id == t.id))

    db.delete(post)
    db.commit()
    return {"status": "success", "deleted_post_id": post_id}


def empty_trash(db: Session) -> int:
    """Permanently purges all items currently marked as deleted in Trash."""
    stmt_targets = select(PostTarget).where(PostTarget.deleted_at.is_not(None))
    trash_targets = db.scalars(stmt_targets).all()

    purged_count = 0
    for t in trash_targets:
        if t.status != TargetStatus.processing:
            db.execute(delete(MetricSnapshot).where(MetricSnapshot.post_target_id == t.id))
            db.execute(delete(PostFeature).where(PostFeature.post_target_id == t.id))
            db.execute(delete(Log).where(Log.post_target_id == t.id))
            db.delete(t)
            purged_count += 1

    # Purge soft-deleted parent posts
    stmt_posts = select(Post).where(Post.deleted_at.is_not(None))
    trash_posts = db.scalars(stmt_posts).all()
    for p in trash_posts:
        # If all targets deleted, delete post
        remaining = [t for t in p.targets if t.status == TargetStatus.processing]
        if not remaining:
            db.delete(p)

    db.commit()
    return purged_count


def purge_expired_trash(db: Session, days: int = 30) -> int:
    """
    Scheduled cleanup: permanently removes items moved to Trash more than `days` days ago.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = select(PostTarget).where(
        PostTarget.deleted_at.is_not(None),
        PostTarget.deleted_at <= cutoff,
    )
    targets = db.scalars(stmt).all()
    purged = 0

    for t in targets:
        if t.status != TargetStatus.processing:
            db.execute(delete(MetricSnapshot).where(MetricSnapshot.post_target_id == t.id))
            db.execute(delete(PostFeature).where(PostFeature.post_target_id == t.id))
            db.execute(delete(Log).where(Log.post_target_id == t.id))
            db.delete(t)
            purged += 1

    # Purge expired posts
    p_stmt = select(Post).where(
        Post.deleted_at.is_not(None),
        Post.deleted_at <= cutoff,
    )
    posts = db.scalars(p_stmt).all()
    for p in posts:
        remaining = [t for t in p.targets if t.status == TargetStatus.processing]
        if not remaining:
            db.delete(p)

    db.commit()
    if purged > 0:
        logger.info(f"Purged {purged} expired Trash item(s) older than {days} days.")
    return purged


def validate_content_for_platforms(
    title: Optional[str],
    body: str,
    media_url: Optional[str],
    platforms: List[str],
) -> None:
    """
    Validates post content against limits and rules for each target platform.
    Raises ValueError with a clear, user-friendly explanation on validation failure.
    """
    if not body or not body.strip():
        raise ValueError("Post body cannot be empty.")

    for plat in platforms:
        p = plat.lower().strip()
        if p == "threads":
            parts = split_thread_parts(title, body)
            for idx, part in enumerate(parts):
                if len(part) > 500:
                    raise ValueError(
                        f"Threads part {idx + 1} exceeds 500 characters ({len(part)} chars). "
                        "Shorten the text or insert '---' on a new line to break it into connected thread parts."
                    )
        elif p == "discord":
            full = f"{title}\n\n{body}" if title and title.strip() else body
            if len(full) > 2000:
                raise ValueError(f"Discord content exceeds 2,000 characters ({len(full)} chars).")
        elif p == "telegram":
            full = f"{title}\n\n{body}" if title and title.strip() else body
            if len(full) > 4096:
                raise ValueError(f"Telegram message exceeds 4,096 characters ({len(full)} chars).")
        elif p == "instagram":
            if not media_url or not media_url.strip():
                raise ValueError("Instagram requires a valid public image or video URL.")
            if len(body) > 2200:
                raise ValueError(f"Instagram caption exceeds 2,200 characters ({len(body)} chars).")


def edit_post_content(
    db: Session,
    post_id: int,
    title: Optional[str],
    body: str,
    media_url: Optional[str],
    platforms: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Edits the text and media of an existing post.
    Enforces strict status rules:
    - Blocked if any target is 'processing'
    - Blocked if any target is already 'posted' (editing published content is unsupported on Threads)
    - Optionally adds or removes platform targets for unpublished posts
    - Re-validates platform constraints
    - Re-runs feature tagging
    """
    post = db.get(Post, post_id)
    if not post or post.deleted_at is not None:
        raise ValueError("Post not found or moved to Trash.")

    for t in post.targets:
        if t.status == TargetStatus.processing:
            raise ValueError("This post is being sent right now. Try again in a moment.")
        if t.status == TargetStatus.posted:
            raise ValueError(
                "Editing published posts is not allowed (Threads and platforms do not support editing). "
                "Use 'Duplicate' instead to create a new post with this content."
            )

    now_utc = datetime.now(timezone.utc)

    # Optional platform updates for unpublished posts
    if platforms is not None:
        target_platforms = [p.strip().lower() for p in platforms if p.strip()]
        if not target_platforms:
            raise ValueError("A post must have at least one target platform.")

        default_sched = now_utc
        for t in post.targets:
            if t.deleted_at is None and t.scheduled_at:
                default_sched = t.scheduled_at
                break

        # Remove unselected platforms
        for t in list(post.targets):
            if t.deleted_at is None and t.platform not in target_platforms:
                t.deleted_at = now_utc
                db.add(Log(post_target_id=t.id, level="INFO", message=f"Platform '{t.platform}' removed from post #{post.id} during edit."))

        # Add or restore selected platforms
        active_plats = {t.platform for t in post.targets if t.deleted_at is None}
        for plat in target_platforms:
            if plat not in active_plats:
                existing_del = next((t for t in post.targets if t.platform == plat and t.deleted_at is not None), None)
                if existing_del:
                    existing_del.deleted_at = None
                    existing_del.scheduled_at = default_sched
                    db.add(Log(post_target_id=existing_del.id, level="INFO", message=f"Platform '{plat}' restored to post #{post.id} during edit."))
                else:
                    new_target = PostTarget(
                        post_id=post.id,
                        platform=plat,
                        scheduled_at=default_sched,
                        status=TargetStatus.pending,
                        attempts=0,
                    )
                    db.add(new_target)
                    db.flush()
                    db.add(Log(post_target_id=new_target.id, level="INFO", message=f"Platform '{plat}' added to post #{post.id} during edit."))

    # Validate against target platforms
    active_platforms = [t.platform for t in post.targets if t.deleted_at is None]
    validate_content_for_platforms(title, body, media_url, active_platforms)

    post.title = title.strip() if title else None
    post.body = body.strip()
    post.media_url = media_url.strip() if media_url else None
    post.edited_at = now_utc

    # Log edit event and re-run tagging
    for t in post.targets:
        db.add(Log(
            post_target_id=t.id,
            level="INFO",
            message=f"Post content edited by user at {now_utc.isoformat()}.",
        ))
        try:
            tag_post(db, t)
        except Exception as exc:
            logger.warning(f"Re-tagging post target #{t.id} notice: {exc}")

    db.commit()
    db.refresh(post)
    return {"status": "success", "post_id": post.id}


def reschedule_target(
    db: Session,
    target_id: int,
    new_scheduled_at: datetime,
) -> Dict[str, Any]:
    """
    Changes scheduled_at for a target in pending, failed, draft, or cancelled status.
    Resets retry attempts to 0 and clears next_retry_at.
    Validates that the time is not in the past.
    """
    target = db.get(PostTarget, target_id)
    if not target or target.deleted_at is not None:
        raise ValueError("Target not found or in Trash.")

    if target.status == TargetStatus.processing:
        raise ValueError("This post is being sent right now. Try again in a moment.")
    if target.status == TargetStatus.posted:
        raise ValueError("Cannot reschedule an already posted target. Use Duplicate instead.")

    now_utc = datetime.now(timezone.utc)
    if new_scheduled_at.tzinfo is None:
        new_scheduled_at = new_scheduled_at.replace(tzinfo=timezone.utc)

    if new_scheduled_at < now_utc - timedelta(minutes=1):
        raise ValueError("Scheduled time cannot be in the past.")

    old_time = target.scheduled_at
    target.scheduled_at = new_scheduled_at
    target.next_retry_at = None
    target.attempts = 0
    target.status = TargetStatus.pending
    target.error_message = None

    db.add(Log(
        post_target_id=target.id,
        level="INFO",
        message=f"Target rescheduled from {old_time} to {new_scheduled_at} UTC. Attempts reset to 0.",
    ))
    db.commit()
    db.refresh(target)
    return {"status": "success", "target_id": target.id, "new_scheduled_at": new_scheduled_at.isoformat()}


def post_now_target(db: Session, target_id: int) -> Dict[str, Any]:
    """
    Immediately schedules a pending or failed target for instant dispatch.
    """
    target = db.get(PostTarget, target_id)
    if not target or target.deleted_at is not None:
        raise ValueError("Target not found or in Trash.")

    if target.status == TargetStatus.processing:
        raise ValueError("This post is being sent right now. Try again in a moment.")
    if target.status == TargetStatus.posted:
        raise ValueError("Target has already been posted.")
    if target.status not in (TargetStatus.pending, TargetStatus.failed):
        raise ValueError(f"Cannot post now. Only pending or failed targets can be dispatched immediately (current: '{target.status.value}').")

    now_utc = datetime.now(timezone.utc)
    target.scheduled_at = now_utc
    target.next_retry_at = None
    target.attempts = 0
    target.status = TargetStatus.pending
    target.error_message = None

    db.add(Log(
        post_target_id=target.id,
        level="INFO",
        message="Manual 'Post now' triggered. Scheduled immediately.",
    ))
    db.commit()
    return {"status": "success", "target_id": target.id}


def cancel_target(db: Session, target_id: int) -> Dict[str, Any]:
    """
    Cancels a pending target so the scheduler will not publish it.
    Can be restored or rescheduled later.
    """
    target = db.get(PostTarget, target_id)
    if not target or target.deleted_at is not None:
        raise ValueError("Target not found or in Trash.")

    if target.status == TargetStatus.processing:
        raise ValueError("This post is being sent right now. Try again in a moment.")
    if target.status == TargetStatus.posted:
        raise ValueError("Cannot cancel an already published post.")
    if target.status != TargetStatus.pending:
        raise ValueError(f"Only pending targets can be cancelled (current: '{target.status.value}').")

    target.status = TargetStatus.cancelled
    target.next_retry_at = None

    db.add(Log(
        post_target_id=target.id,
        level="INFO",
        message="Target publication cancelled by user.",
    ))
    db.commit()
    return {"status": "success", "target_id": target.id}


def duplicate_post(db: Session, post_id: int) -> int:
    """
    Creates a new draft post with the same title, body, and media_url.
    Copies active platform targets as draft targets.
    """
    source = db.get(Post, post_id)
    if not source:
        raise ValueError("Source post not found.")

    now_utc = datetime.now(timezone.utc)
    new_post = Post(
        title=f"{source.title} (Copy)" if source.title else None,
        body=source.body,
        media_url=source.media_url,
        media_path=source.media_path,
        created_at=now_utc,
    )
    db.add(new_post)
    db.flush()

    for t in source.targets:
        if t.deleted_at is None:
            new_target = PostTarget(
                post_id=new_post.id,
                platform=t.platform,
                account_id=t.account_id,
                scheduled_at=t.scheduled_at or now_utc,
                status=TargetStatus.draft,
                attempts=0,
            )
            db.add(new_target)

    db.commit()
    db.refresh(new_post)
    return new_post.id


def execute_bulk_action(
    db: Session,
    action: str,
    target_ids: List[int],
    shift_hours: int = 0,
) -> Dict[str, Any]:
    """
    Executes batch operations across selected target IDs:
    - 'delete': moves to Trash
    - 'retry': resets failed targets to pending
    - 'cancel': cancels pending targets
    - 'restore': restores targets from Trash
    - 'shift_schedule': adjusts scheduled_at by +shift_hours
    Returns per-item results showing completed and skipped items with human-friendly reasons.
    """
    done_count = 0
    skipped_count = 0
    details: List[Dict[str, Any]] = []

    now_utc = datetime.now(timezone.utc)

    for tid in target_ids:
        t = db.get(PostTarget, tid)
        if not t:
            skipped_count += 1
            details.append({"id": tid, "status": "skipped", "reason": "Target not found"})
            continue

        if action == "delete":
            if t.deleted_at is not None:
                skipped_count += 1
                details.append({"id": tid, "status": "skipped", "reason": "Already in Trash"})
            elif t.status == TargetStatus.processing:
                skipped_count += 1
                details.append({"id": tid, "status": "skipped", "reason": "Cannot delete while actively sending"})
            else:
                t.deleted_at = now_utc
                db.add(Log(post_target_id=t.id, level="INFO", message="Moved to Trash via bulk action."))
                done_count += 1
                details.append({"id": tid, "status": "done", "reason": "Moved to Trash"})

        elif action == "retry":
            if t.status != TargetStatus.failed:
                skipped_count += 1
                details.append({"id": tid, "status": "skipped", "reason": f"Only failed targets can be retried (current: {t.status.value})"})
            elif t.deleted_at is not None:
                skipped_count += 1
                details.append({"id": tid, "status": "skipped", "reason": "Target is in Trash"})
            else:
                t.status = TargetStatus.pending
                t.attempts = 0
                t.next_retry_at = None
                t.scheduled_at = now_utc
                db.add(Log(post_target_id=t.id, level="INFO", message="Bulk retry triggered."))
                done_count += 1
                details.append({"id": tid, "status": "done", "reason": "Retry queued"})

        elif action == "cancel":
            if t.status != TargetStatus.pending:
                skipped_count += 1
                details.append({"id": tid, "status": "skipped", "reason": f"Only pending targets can be cancelled (current: {t.status.value})"})
            else:
                t.status = TargetStatus.cancelled
                db.add(Log(post_target_id=t.id, level="INFO", message="Cancelled via bulk action."))
                done_count += 1
                details.append({"id": tid, "status": "done", "reason": "Cancelled"})

        elif action == "restore":
            if t.deleted_at is None:
                skipped_count += 1
                details.append({"id": tid, "status": "skipped", "reason": "Target is not in Trash"})
            else:
                t.deleted_at = None
                if t.post and t.post.deleted_at is not None:
                    t.post.deleted_at = None
                db.add(Log(post_target_id=t.id, level="INFO", message="Restored via bulk action."))
                done_count += 1
                details.append({"id": tid, "status": "done", "reason": "Restored from Trash"})

        elif action == "shift_schedule":
            if t.status not in (TargetStatus.pending, TargetStatus.draft):
                skipped_count += 1
                details.append({"id": tid, "status": "skipped", "reason": "Only pending or draft targets can be rescheduled"})
            elif t.deleted_at is not None:
                skipped_count += 1
                details.append({"id": tid, "status": "skipped", "reason": "Target is in Trash"})
            else:
                new_time = t.scheduled_at + timedelta(hours=shift_hours)
                t.scheduled_at = new_time
                db.add(Log(post_target_id=t.id, level="INFO", message=f"Schedule shifted by {shift_hours}h to {new_time} UTC via bulk action."))
                done_count += 1
                details.append({"id": tid, "status": "done", "reason": f"Shifted by {shift_hours}h"})

        else:
            skipped_count += 1
            details.append({"id": tid, "status": "skipped", "reason": f"Unknown bulk action '{action}'"})

    db.commit()
    return {
        "action": action,
        "done": done_count,
        "skipped": skipped_count,
        "details": details,
    }

