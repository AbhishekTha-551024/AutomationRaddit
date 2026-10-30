"""
Development-only API endpoints.
Provides manual testing utilities during local development.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.config import settings
from app.database import get_db
from app.models.account import Account
from app.services.threads_token_service import ensure_fresh_token
from app.adapters.telegram import TelegramAdapter
from app.adapters.discord import DiscordAdapter
from app.adapters.threads import ThreadsAdapter
from app.adapters.instagram import InstagramAdapter
from app.adapters.base import PublishResult
from app.services.instagram_token_service import inspect_instagram_account

router = APIRouter(tags=["Development Testing"])


class DevTestRequest(BaseModel):
    """Payload for manual platform test publishing."""
    title: Optional[str] = None
    body: str
    media_url: Optional[str] = None
    image_url: Optional[str] = None


# Keep TelegramTestRequest alias for backward compatibility
TelegramTestRequest = DevTestRequest


@dataclass
class InMemoryPost:
    """Simple in-memory post object with no database persistence."""
    title: Optional[str]
    body: str
    media_url: Optional[str] = None


@router.post("/dev/test-telegram")
def test_telegram_publish(payload: DevTestRequest) -> dict:
    """
    Test endpoint for Telegram publication.
    Constructs an in-memory post and dispatches it via TelegramAdapter.
    Only enabled in development environment.
    """
    post = InMemoryPost(title=payload.title, body=payload.body)
    adapter = TelegramAdapter()
    result: PublishResult = adapter.publish(post)

    return {
        "success": result.success,
        "external_post_id": result.external_post_id,
        "error_message": result.error_message,
        "retryable": result.retryable,
    }


@router.post("/dev/test-discord")
def test_discord_publish(payload: DevTestRequest) -> dict:
    """
    Test endpoint for Discord publication.
    Constructs an in-memory post and dispatches it via DiscordAdapter.
    Only enabled in development environment.
    """
    post = InMemoryPost(title=payload.title, body=payload.body)
    adapter = DiscordAdapter()
    result: PublishResult = adapter.publish(post)

    return {
        "success": result.success,
        "external_post_id": result.external_post_id,
        "error_message": result.error_message,
        "retryable": result.retryable,
    }


@router.post("/dev/test-threads")
def test_threads_publish(payload: DevTestRequest) -> dict:
    """
    Test endpoint for Threads publication.
    Constructs an in-memory post and dispatches it via ThreadsAdapter.
    Only enabled in development environment.
    """
    post = InMemoryPost(title=payload.title, body=payload.body)
    adapter = ThreadsAdapter()
    result: PublishResult = adapter.publish(post)

    return {
        "success": result.success,
        "external_post_id": result.external_post_id,
        "error_message": result.error_message,
        "retryable": result.retryable,
    }


@router.get("/dev/threads-account")
def get_threads_account_status(db: Session = Depends(get_db)) -> dict:
    """
    Returns Threads account authentication and token status.
    Never exposes raw tokens, secrets, or encryption keys.
    """
    now = datetime.now(timezone.utc)

    # If no account exists yet, try initializing if token is configured in .env
    account = db.scalars(select(Account).where(Account.platform == "threads")).first()
    if not account and settings.THREADS_ACCESS_TOKEN:
        try:
            info = ensure_fresh_token(db)
            expiry = info.get("token_expiry")
            days_left = (expiry - now).total_seconds() / 86400 if expiry else None
            return {
                "username": info.get("username"),
                "token_expiry": expiry.isoformat() if expiry else None,
                "is_expired": False,
                "days_remaining": round(days_left, 1) if days_left is not None else None,
            }
        except Exception as exc:
            return {
                "username": None,
                "token_expiry": None,
                "is_expired": True,
                "days_remaining": 0.0,
                "error": str(exc),
            }

    if not account:
        return {
            "username": None,
            "token_expiry": None,
            "is_expired": True,
            "days_remaining": 0.0,
            "message": "No Threads account configured. Add THREADS_ACCESS_TOKEN and ENCRYPTION_KEY to .env.",
        }

    expiry = account.token_expiry
    if expiry and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)

    is_expired = expiry is not None and expiry <= now
    days_left = (expiry - now).total_seconds() / 86400 if expiry else None

    return {
        "username": account.name,
        "token_expiry": expiry.isoformat() if expiry else None,
        "is_expired": is_expired,
        "days_remaining": round(days_left, 1) if days_left is not None else None,
    }


@router.post("/dev/test-instagram")
def test_instagram_publish(payload: DevTestRequest) -> dict:
    """
    Test endpoint for Instagram publication.
    Constructs an in-memory post with public image/video URL and dispatches via InstagramAdapter.
    Only enabled in development environment.
    """
    target_media = payload.image_url or payload.media_url
    post = InMemoryPost(title=payload.title, body=payload.body, media_url=target_media)
    adapter = InstagramAdapter()
    result: PublishResult = adapter.publish(post)

    return {
        "success": result.success,
        "external_post_id": result.external_post_id,
        "error_message": result.error_message,
        "retryable": result.retryable,
    }


@router.get("/dev/instagram-account")
def get_instagram_account_status() -> dict:
    """
    Returns Instagram account diagnostics, linked Facebook Pages,
    Instagram Business Account ID, and all granted Meta Developer permissions.
    Never exposes raw access tokens or secrets.
    """
    token = settings.INSTAGRAM_ACCESS_TOKEN
    if not token or not token.strip():
        return {
            "status": "unconfigured",
            "message": "No Instagram access token configured. Add INSTAGRAM_ACCESS_TOKEN to .env.",
        }

    override_id = settings.INSTAGRAM_ACCOUNT_ID
    return inspect_instagram_account(token, override_id)


@router.post("/dev/threads-reload-token")
def reload_threads_token_endpoint(db: Session = Depends(get_db)):
    """
    Re-reads THREADS_ACCESS_TOKEN from .env, exchanges it for a 60-day token,
    and updates the encrypted credentials in the database.
    Never returns or leaks the raw token.
    """
    try:
        from app.services.threads_token_service import reload_threads_token
        res = reload_threads_token(db)
        return {
            "status": "success",
            "username": res["username"],
            "token_expiry": res["token_expiry"].isoformat() if res.get("token_expiry") else None,
            "message": "Threads token successfully reloaded and encrypted in DB.",
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/dev/collect-metrics")
def trigger_collect_metrics(db: Session = Depends(get_db)):
    """
    Manually triggers metrics collection for due age buckets on posted Threads targets.
    Returns the number of snapshots captured.
    """
    from app.services.metrics_collector import collect_due_snapshots
    saved = collect_due_snapshots(db)
    return {
        "status": "success",
        "saved_snapshots": saved,
    }


@router.post("/dev/collect-account-metrics")
def trigger_collect_account_metrics(db: Session = Depends(get_db)):
    """
    Manually triggers daily account-level audience metrics collection.
    """
    from app.services.metrics_collector import collect_account_snapshot
    snap = collect_account_snapshot(db)
    if snap:
        return {
            "status": "success",
            "snapshot_date": str(snap.snapshot_date),
            "followers_count": snap.followers_count,
            "views": snap.views,
            "likes": snap.likes,
            "replies": snap.replies,
        }
    return {
        "status": "skipped_or_failed",
        "message": "Snapshot already exists for today or collection failed. Check server logs.",
    }


@router.post("/dev/backfill-tags")
def trigger_backfill_tags(db: Session = Depends(get_db)):
    """
    Backfills rule-based structural and lexical features for all posted Threads targets.
    """
    from app.services.tagging import backfill_all_tags
    count = backfill_all_tags(db)
    return {
        "status": "success",
        "tagged_targets": count,
    }


@router.post("/dev/run-analyst")
def trigger_run_analyst(db: Session = Depends(get_db)):
    """
    Manually triggers the weekly AI Analyst (budget-guarded).
    """
    from app.services.analyst import run_ai_analyst
    return run_ai_analyst(db)


@router.post("/dev/telegram-find-chat")
def dev_find_telegram_chat(db: Session = Depends(get_db)) -> dict:
    """
    Calls Telegram getUpdates to discover recent private chats that messaged the bot.
    Returns ONLY chat_id, chat_type, and first_name/username. Never leaks tokens.
    """
    from app.services.alerts import get_recent_private_chats
    from app.services.connection_service import get_platform_credentials

    creds = get_platform_credentials("telegram", db)
    bot_token = creds.get("bot_token")
    if not bot_token:
        return {
            "status": "error",
            "message": "No Telegram bot token configured. Set it in /connections or .env.",
            "chats": [],
        }

    chats = get_recent_private_chats(bot_token)
    return {
        "status": "success",
        "count": len(chats),
        "chats": chats,
        "instructions": "If your chat is not listed, open Telegram, send /start to your bot, and call this endpoint again.",
    }


@router.post("/dev/test-alert")
def dev_send_test_alert(db: Session = Depends(get_db)) -> dict:
    """
    Dispatches a test failure alert to the configured private Telegram chat.
    """
    from app.services.alerts import send_alert

    alert_chat_id = settings.TELEGRAM_ALERT_CHAT_ID
    if not alert_chat_id:
        return {
            "status": "error",
            "message": "TELEGRAM_ALERT_CHAT_ID is not configured in .env. Use /dev/telegram-find-chat to find your chat ID.",
        }

    ok = send_alert(
        kind="test_alert",
        title="Test Alert from Social Media Scheduler",
        body="This is a test notification confirming your Telegram private alert connection is working properly.",
        dedupe_key=None,
        db=db,
    )

    if ok:
        return {
            "status": "success",
            "message": f"Test alert dispatched to Telegram chat {alert_chat_id}.",
        }
    else:
        return {
            "status": "failed",
            "message": "Could not send test alert. Ensure alerts are enabled, bot token is valid, and chat ID is correct.",
        }




