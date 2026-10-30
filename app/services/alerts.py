"""
Alert service for dispatching failure notifications to a private Telegram chat.
Includes deduplication, alert storm grouping, quiet hours, rate limiting,
and sanitized plain-text message formatting.
"""

from datetime import datetime, timezone, timedelta, time
import logging
from typing import Optional, Dict, Any, List
import httpx
from sqlalchemy import select, func, and_
from sqlalchemy.orm import Session
from app.config import settings
from app.database import SessionLocal
from app.models.alert import AlertLog
from app.models.setting import AppSetting
from app.models.target import PostTarget, TargetStatus
from app.models.account import Account
from app.services.connection_service import get_platform_credentials, mask_credential_value

logger = logging.getLogger(__name__)

# IST timezone offset (+05:30)
IST_TZ = timezone(timedelta(hours=5, minutes=30))


def is_alerts_enabled(db: Optional[Session] = None) -> bool:
    """
    Checks if alerts are globally enabled.
    Checks `app_settings` for 'alerts_enabled' first, falling back to settings.ALERTS_ENABLED.
    """
    if db is not None:
        setting = db.get(AppSetting, "alerts_enabled")
        if setting:
            return setting.value.strip().lower() in ("true", "1", "yes")

    # If no db passed, open temporary session
    try:
        with SessionLocal() as session:
            setting = session.get(AppSetting, "alerts_enabled")
            if setting:
                return setting.value.strip().lower() in ("true", "1", "yes")
    except Exception:
        pass

    return bool(settings.ALERTS_ENABLED)


def is_in_quiet_hours() -> bool:
    """
    Determines if current wall-clock time in IST is within quiet hours.
    Returns False if quiet hours are not configured.
    """
    start_str = settings.ALERT_QUIET_HOURS_START
    end_str = settings.ALERT_QUIET_HOURS_END

    if not start_str or not end_str or not start_str.strip() or not end_str.strip():
        return False

    try:
        now_ist = datetime.now(timezone.utc).astimezone(IST_TZ).time()
        start_parts = [int(p) for p in start_str.strip().split(":")]
        end_parts = [int(p) for p in end_str.strip().split(":")]

        t_start = time(start_parts[0], start_parts[1])
        t_end = time(end_parts[0], end_parts[1])

        if t_start > t_end:
            # Spans midnight, e.g. 23:00 to 07:00
            return now_ist >= t_start or now_ist < t_end
        else:
            return t_start <= now_ist < t_end
    except Exception as exc:
        logger.warning(f"Error parsing quiet hours ({start_str} - {end_str}): {exc}")
        return False


def send_alert(
    kind: str,
    title: str,
    body: str,
    dedupe_key: Optional[str] = None,
    db: Optional[Session] = None,
) -> bool:
    """
    Sends an alert notification to the user's private Telegram chat.
    Guarantees:
    - Never throws or crashes calling code (publishing or scheduler).
    - Deduplication: checks `alert_log` so identical alerts within cooldown aren't repeated.
    - Grouping: collapses storm of > 3 failed posts in 10 minutes into a single notice.
    - Quiet hours: queues/suppresses non-critical alerts during configured night hours.
    - Rate limiting: caps dispatch at 20 alerts per hour.
    - Zero secrets: formats all messages in plain text with no tokens, secrets, or URLs.
    """
    try:
        if db is not None:
            return _execute_send_alert(kind, title, body, dedupe_key, db)
        else:
            with SessionLocal() as session:
                return _execute_send_alert(kind, title, body, dedupe_key, session)
    except Exception as exc:
        logger.error(f"Failed to dispatch alert ({kind}): {exc}")
        return False


def _execute_send_alert(
    kind: str,
    title: str,
    body: str,
    dedupe_key: Optional[str],
    db: Session,
) -> bool:
    # 1. Check if alerts are enabled
    if not is_alerts_enabled(db):
        logger.debug(f"Alert '{kind}' skipped: alerts are disabled.")
        return False

    # 2. Check destination chat ID
    alert_chat_id = (settings.TELEGRAM_ALERT_CHAT_ID or "").strip()
    if not alert_chat_id:
        logger.debug(f"Alert '{kind}' skipped: TELEGRAM_ALERT_CHAT_ID is not configured.")
        return False

    now_utc = datetime.now(timezone.utc)

    # 3. Deduplication check
    if dedupe_key:
        # For failed posts, alert is per-target and sent only once
        if kind == "failed_post":
            existing = db.scalars(
                select(AlertLog).where(
                    AlertLog.dedupe_key == dedupe_key,
                    AlertLog.success.is_(True),
                )
            ).first()
            if existing:
                logger.debug(f"Alert '{kind}' skipped by dedupe key: {dedupe_key}")
                return False
        else:
            # Default cooldown: 6 hours
            cooldown_cutoff = now_utc - timedelta(hours=6)
            existing = db.scalars(
                select(AlertLog).where(
                    AlertLog.dedupe_key == dedupe_key,
                    AlertLog.sent_at >= cooldown_cutoff,
                    AlertLog.success.is_(True),
                )
            ).first()
            if existing:
                logger.debug(f"Alert '{kind}' suppressed by 6h cooldown dedupe key: {dedupe_key}")
                return False

    # 4. Hourly Rate Limiting (at most 20 alerts per hour)
    one_hour_ago = now_utc - timedelta(hours=1)
    hourly_count = db.scalar(
        select(func.count(AlertLog.id)).where(
            AlertLog.sent_at >= one_hour_ago,
            AlertLog.success.is_(True),
        )
    ) or 0

    if hourly_count >= 20:
        logger.warning(f"Alert rate limit reached (20/hr). Suppressing alert: {title}")
        db.add(AlertLog(
            kind=kind,
            dedupe_key=dedupe_key,
            sent_at=now_utc,
            success=False,
            error="Hourly rate limit exceeded (max 20/hr)",
            message_preview=title[:100],
        ))
        db.commit()
        return False

    # 5. Alert Grouping: If > 3 failed alerts would fire within 10 minutes
    if kind == "failed_post":
        ten_minutes_ago = now_utc - timedelta(minutes=10)
        recent_failures_count = db.scalar(
            select(func.count(AlertLog.id)).where(
                AlertLog.kind == "failed_post",
                AlertLog.sent_at >= ten_minutes_ago,
                AlertLog.success.is_(True),
            )
        ) or 0

        if recent_failures_count >= 3:
            # Check if a grouped alert was already dispatched in the last 10 minutes
            grouped_key = f"grouped_failures:{now_utc.strftime('%Y%m%d%H%M')[:11]}"
            grouped_exists = db.scalars(
                select(AlertLog).where(
                    AlertLog.dedupe_key.like("grouped_failures%"),
                    AlertLog.sent_at >= ten_minutes_ago,
                )
            ).first()

            if not grouped_exists:
                # Dispatch grouped alert message instead
                title = "Multiple post failures detected"
                body = f"{recent_failures_count + 1} posts failed in the last 10 minutes. Check your dashboard at {settings.BASE_URL}/posts for details."
                dedupe_key = grouped_key
            else:
                # Already sent a grouped notice within this 10-minute window
                logger.debug(f"Storm suppression: already sent grouped failure alert.")
                return False

    # 6. Quiet hours check
    # Critical alerts (failed_post and token_expiring/expired) still send immediately
    is_critical = (kind in ("failed_post", "token_expired", "token_expiring"))
    if not is_critical and is_in_quiet_hours():
        logger.info(f"Non-critical alert '{kind}' suppressed during quiet hours.")
        db.add(AlertLog(
            kind=kind,
            dedupe_key=dedupe_key,
            sent_at=now_utc,
            success=False,
            error="Suppressed during quiet hours",
            message_preview=title[:100],
        ))
        db.commit()
        return False

    # 7. Get Telegram Bot Token
    creds = get_platform_credentials("telegram", db)
    bot_token = (creds.get("bot_token") or "").strip()
    if not bot_token:
        logger.warning("Telegram bot token not configured; cannot send alert.")
        return False

    # 8. Compose Plain-Text Message (sentence case, zero secrets)
    now_ist_str = now_utc.astimezone(IST_TZ).strftime("%d %b %Y, %I:%M %p IST")
    message_text = f"🚨 {title}\n\n{body}\n\nTime: {now_ist_str}"

    # 9. Dispatch to Telegram
    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": alert_chat_id,
        "text": message_text,
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(api_url, json=payload)
            data = resp.json()

        if resp.status_code == 200 and data.get("ok"):
            db.add(AlertLog(
                kind=kind,
                dedupe_key=dedupe_key,
                sent_at=now_utc,
                success=True,
                error=None,
                message_preview=message_text[:150],
            ))
            db.commit()
            logger.info(f"Alert '{kind}' sent to private Telegram chat ({alert_chat_id}).")
            return True
        else:
            err_desc = data.get("description", f"HTTP {resp.status_code}")
            logger.warning(f"Telegram alert send failed: {err_desc}")
            db.add(AlertLog(
                kind=kind,
                dedupe_key=dedupe_key,
                sent_at=now_utc,
                success=False,
                error=f"Telegram API: {err_desc}",
                message_preview=message_text[:150],
            ))
            db.commit()
            return False

    except Exception as exc:
        err_msg = str(exc)
        logger.warning(f"Network error sending Telegram alert: {err_msg}")
        db.add(AlertLog(
            kind=kind,
            dedupe_key=dedupe_key,
            sent_at=now_utc,
            success=False,
            error=f"Network error: {err_msg}",
            message_preview=message_text[:150],
        ))
        db.commit()
        return False


def get_recent_private_chats(bot_token: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Calls Telegram getUpdates to discover recent private chats that messaged the bot.
    Returns only chat_id, type, first_name, and username. Strictly never returns bot token.
    """
    token = (bot_token or "").strip()
    if not token:
        # Fetch from DB or env
        creds = get_platform_credentials("telegram")
        token = (creds.get("bot_token") or "").strip()

    if not token:
        return []

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url)
            data = resp.json()

        if resp.status_code != 200 or not data.get("ok"):
            logger.warning(f"getUpdates failed: {data.get('description')}")
            return []

        updates = data.get("result", [])
        chats_seen = {}
        for update in reversed(updates):
            msg = update.get("message") or update.get("channel_post") or update.get("my_chat_member")
            if msg:
                chat = msg.get("chat", {})
                chat_id = chat.get("id")
                if chat_id and chat_id not in chats_seen:
                    chats_seen[chat_id] = {
                        "chat_id": str(chat_id),
                        "chat_type": chat.get("type", "unknown"),
                        "first_name": chat.get("first_name") or chat.get("title") or "User",
                        "username": chat.get("username"),
                    }

        return list(chats_seen.values())

    except Exception as exc:
        logger.warning(f"Error querying Telegram getUpdates: {exc}")
        return []


def check_token_expiries(db: Session) -> None:
    """
    Checks token expiries for Threads and Instagram accounts.
    Fires alerts when:
    - Token expires within 7 days
    - Token expires within 2 days
    - Token has expired
    """
    now_utc = datetime.now(timezone.utc)
    accounts = db.scalars(
        select(Account).where(
            Account.platform.in_(["threads", "instagram"]),
            Account.token_expiry.is_not(None),
        )
    ).all()

    for acc in accounts:
        expiry = acc.token_expiry
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)

        diff = expiry - now_utc
        days = diff.days

        if diff.total_seconds() <= 0:
            send_alert(
                kind="token_expired",
                title=f"{acc.platform.capitalize()} token expired",
                body=f"The access token for {acc.name} has expired. Posts will fail until updated in Connections.",
                dedupe_key=f"token_expired:{acc.platform}",
                db=db,
            )
        elif days <= 2:
            send_alert(
                kind="token_expiring",
                title=f"{acc.platform.capitalize()} token expires in {max(1, days)} day(s)",
                body=f"The access token for {acc.name} expires soon. Please refresh or update credentials in Connections.",
                dedupe_key=f"token_expiring_2d:{acc.platform}",
                db=db,
            )
        elif days <= 7:
            send_alert(
                kind="token_expiring",
                title=f"{acc.platform.capitalize()} token expires in {days} days",
                body=f"The access token for {acc.name} expires in {days} days. Please refresh or update credentials in Connections.",
                dedupe_key=f"token_expiring_7d:{acc.platform}",
                db=db,
            )


def check_paused_and_missed_posts(db: Session) -> None:
    """
    Alerts if:
    - Publishing has been paused for > 2 hours while pending posts exist.
    - Overdue posts (older than OVERDUE_GRACE_HOURS) are pending.
    """
    now_utc = datetime.now(timezone.utc)

    # 1. Check paused > 2h
    setting = db.get(AppSetting, "publishing_paused")
    if setting and setting.value.strip().lower() in ("true", "1", "yes"):
        updated = setting.updated_at
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)

        if now_utc - updated >= timedelta(hours=2):
            pending_count = db.scalar(
                select(func.count(PostTarget.id)).where(
                    PostTarget.status == TargetStatus.pending,
                    PostTarget.deleted_at.is_(None),
                )
            ) or 0
            if pending_count > 0:
                send_alert(
                    kind="paused_too_long",
                    title="Publishing paused for over 2 hours",
                    body=f"There are {pending_count} scheduled post(s) waiting. Resume publishing from the dashboard when ready.",
                    dedupe_key=f"paused_2h:{updated.strftime('%Y%m%d%H')}",
                    db=db,
                )

    # 2. Check missed overdue posts beyond grace period
    grace_cutoff = now_utc - timedelta(hours=settings.OVERDUE_GRACE_HOURS)
    missed_count = db.scalar(
        select(func.count(PostTarget.id)).where(
            PostTarget.status == TargetStatus.pending,
            PostTarget.deleted_at.is_(None),
            PostTarget.scheduled_at < grace_cutoff,
        )
    ) or 0

    if missed_count > 0:
        send_alert(
            kind="missed_posts",
            title="Overdue posts detected",
            body=f"{missed_count} post(s) are overdue beyond {settings.OVERDUE_GRACE_HOURS} hours. Review and confirm them on your dashboard.",
            dedupe_key=f"missed_posts:{now_utc.strftime('%Y%m%d%H')}",
            db=db,
        )


def check_ai_budget_alert(db: Session) -> None:
    """
    Alerts if AI monthly usage reaches 80% or 100% of the configured budget.
    """
    budget = settings.AI_MONTHLY_BUDGET_USD
    if budget <= 0:
        return

    now_utc = datetime.now(timezone.utc)
    month_start = datetime(now_utc.year, now_utc.month, 1, tzinfo=timezone.utc)

    try:
        from app.models.analyst import UsageLog
        total_spent = db.scalar(
            select(func.coalesce(func.sum(UsageLog.cost_usd), 0.0)).where(
                UsageLog.created_at >= month_start
            )
        ) or 0.0

        month_key = now_utc.strftime("%Y%m")
        if total_spent >= budget:
            send_alert(
                kind="budget",
                title="AI monthly budget limit reached (100%)",
                body=f"AI Analyst has reached the ${budget:.2f} monthly budget (${total_spent:.2f} spent). Runs will be paused until next month.",
                dedupe_key=f"budget_100:{month_key}",
                db=db,
            )
        elif total_spent >= 0.8 * budget:
            send_alert(
                kind="budget",
                title="AI monthly budget at 80%",
                body=f"AI Analyst has used 80% of its monthly budget (${total_spent:.2f} of ${budget:.2f}).",
                dedupe_key=f"budget_80:{month_key}",
                db=db,
            )
    except Exception as exc:
        logger.debug(f"Budget check notice: {exc}")


def send_daily_digest(db: Session) -> None:
    """
    Sends the daily digest to Telegram:
    - Posts published yesterday
    - Posts failed
    - Posts scheduled for today
    - Top post by engagement score
    Skips the digest if there is nothing to report.
    """
    now_utc = datetime.now(timezone.utc)
    yesterday_utc = now_utc - timedelta(days=1)
    today_start = datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc)
    yesterday_start = today_start - timedelta(days=1)

    # 1. Posted yesterday
    posted_count = db.scalar(
        select(func.count(PostTarget.id)).where(
            PostTarget.status == TargetStatus.posted,
            PostTarget.posted_at >= yesterday_start,
            PostTarget.posted_at < today_start,
            PostTarget.deleted_at.is_(None),
        )
    ) or 0

    # 2. Failed yesterday or today
    failed_count = db.scalar(
        select(func.count(PostTarget.id)).where(
            PostTarget.status == TargetStatus.failed,
            PostTarget.deleted_at.is_(None),
        )
    ) or 0

    # 3. Scheduled for today
    today_end = today_start + timedelta(days=1)
    scheduled_today_count = db.scalar(
        select(func.count(PostTarget.id)).where(
            PostTarget.status == TargetStatus.pending,
            PostTarget.scheduled_at >= today_start,
            PostTarget.scheduled_at < today_end,
            PostTarget.deleted_at.is_(None),
        )
    ) or 0

    # 4. Skip if nothing to report
    if posted_count == 0 and failed_count == 0 and scheduled_today_count == 0:
        logger.info("Daily digest skipped: nothing to report.")
        return

    # Top post by engagement score (if analytics service exists)
    top_post_str = "None"
    try:
        from app.services.analytics import get_top_posts
        top_posts = get_top_posts(db, limit=1)
        if top_posts:
            tp = top_posts[0]
            top_post_str = f"'{tp.get('title') or 'Post'}' (Score: {tp.get('score', 0):.1f})"
    except Exception:
        pass

    digest_body = (
        f"• Published yesterday: {posted_count}\n"
        f"• Failed posts: {failed_count}\n"
        f"• Scheduled today: {scheduled_today_count}\n"
        f"• Top performer: {top_post_str}\n\n"
        f"View your calendar and posts at {settings.BASE_URL}/posts."
    )

    send_alert(
        kind="daily_digest",
        title="Daily publishing digest",
        body=digest_body,
        dedupe_key=f"digest:{now_utc.strftime('%Y%m%d')}",
        db=db,
    )
