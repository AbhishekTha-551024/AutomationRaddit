"""
API Router for platform connections, health checks, credential updates, and alert settings.
Guarantees zero secret leakage in JSON responses.
"""

from typing import Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Body
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.setting import AppSetting
from app.services.connection_service import (
    test_platform_connection,
    save_platform_credentials,
    get_all_connections_summary,
)
from app.services.alerts import send_alert, is_alerts_enabled
from app.config import settings

router = APIRouter(prefix="", tags=["Connections & Settings"])


class UpdateCredentialsRequest(BaseModel):
    """Payload for updating platform credentials. Fields are optional depending on platform."""
    # Telegram
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None

    # Discord
    webhook_url: Optional[str] = None

    # Threads
    access_token: Optional[str] = None
    app_secret: Optional[str] = None

    # Instagram
    account_id: Optional[str] = None


@router.post("/connections/{platform}/test")
def test_connection_endpoint(
    platform: str,
    db: Session = Depends(get_db),
) -> dict:
    """
    Runs a safe, read-only connection test on the specified platform.
    Never posts anything publicly and never returns secrets.
    """
    norm_plat = platform.strip().lower()
    if norm_plat not in ("telegram", "discord", "threads", "instagram"):
        raise HTTPException(status_code=400, detail=f"Unsupported platform '{platform}'.")

    result = test_platform_connection(norm_plat, db)
    return {
        "ok": result["ok"],
        "platform": norm_plat,
        "message": result["message"],
        "suggestion": result.get("suggestion"),
        "identity": result.get("identity"),
    }


@router.post("/connections/{platform}/update")
def update_credentials_endpoint(
    platform: str,
    payload: UpdateCredentialsRequest,
    db: Session = Depends(get_db),
) -> dict:
    """
    Validates new credentials with a read-only test FIRST:
    If test fails: keeps old credentials and returns error with suggestion.
    If test passes: encrypts with Fernet, saves to accounts table, and updates connection status.
    Strictly never logs, prints, or returns secrets.
    """
    norm_plat = platform.strip().lower()
    if norm_plat not in ("telegram", "discord", "threads", "instagram"):
        raise HTTPException(status_code=400, detail=f"Unsupported platform '{platform}'.")

    raw_data: Dict[str, Any] = {}
    if norm_plat == "telegram":
        if not payload.bot_token or not payload.chat_id:
            raise HTTPException(status_code=422, detail="Both 'bot_token' and 'chat_id' are required for Telegram.")
        raw_data = {"bot_token": payload.bot_token, "chat_id": payload.chat_id}

    elif norm_plat == "discord":
        if not payload.webhook_url:
            raise HTTPException(status_code=422, detail="'webhook_url' is required for Discord.")
        raw_data = {"webhook_url": payload.webhook_url}

    elif norm_plat == "threads":
        if not payload.access_token:
            raise HTTPException(status_code=422, detail="'access_token' is required for Threads.")
        raw_data = {"access_token": payload.access_token, "app_secret": payload.app_secret}

    elif norm_plat == "instagram":
        if not payload.access_token:
            raise HTTPException(status_code=422, detail="'access_token' is required for Instagram.")
        raw_data = {"access_token": payload.access_token, "account_id": payload.account_id}

    result = save_platform_credentials(db, norm_plat, raw_data)
    if not result["ok"]:
        return {
            "ok": False,
            "platform": norm_plat,
            "message": result["message"],
            "suggestion": result.get("suggestion"),
        }

    return {
        "ok": True,
        "platform": norm_plat,
        "message": result["message"],
        "identity": result.get("identity"),
    }


@router.post("/connections/{platform}/refresh")
def refresh_token_endpoint(
    platform: str,
    db: Session = Depends(get_db),
) -> dict:
    """
    Refreshes expiring access token for platforms that support proactive refresh (Threads, Instagram).
    """
    norm_plat = platform.strip().lower()
    if norm_plat == "threads":
        from app.services.threads_token_service import reload_threads_token
        res = reload_threads_token(db)
        return {
            "ok": True,
            "platform": "threads",
            "message": f"Threads token refreshed for @{res.get('username')}.",
            "expiry": res.get("token_expiry"),
        }
    elif norm_plat == "instagram":
        from app.services.instagram_token_service import ensure_fresh_instagram_token
        res = ensure_fresh_instagram_token(db, force=True)
        return {
            "ok": True,
            "platform": "instagram",
            "message": f"Instagram credentials verified for {res.get('username')}.",
        }
    else:
        raise HTTPException(status_code=400, detail=f"Platform '{platform}' does not support token refresh.")


@router.post("/settings/alerts/toggle")
def toggle_alerts_endpoint(db: Session = Depends(get_db)) -> dict:
    """
    Toggles the alert dispatching flag between enabled and disabled in app_settings.
    """
    setting = db.get(AppSetting, "alerts_enabled")
    current_enabled = is_alerts_enabled(db)
    new_state = not current_enabled

    if not setting:
        setting = AppSetting(key="alerts_enabled", value="true" if new_state else "false")
        db.add(setting)
    else:
        setting.value = "true" if new_state else "false"

    db.commit()
    return {
        "status": "success",
        "alerts_enabled": new_state,
        "message": f"Alerts are now {'enabled' if new_state else 'disabled'}.",
    }


@router.post("/settings/alerts/test")
def trigger_test_alert_endpoint(db: Session = Depends(get_db)) -> dict:
    """
    Dispatches a manual test alert notification to verify Telegram setup.
    """
    alert_chat_id = settings.TELEGRAM_ALERT_CHAT_ID
    if not alert_chat_id:
        raise HTTPException(
            status_code=400,
            detail="TELEGRAM_ALERT_CHAT_ID is not configured. Find your chat ID with POST /dev/telegram-find-chat and add it to .env.",
        )

    ok = send_alert(
        kind="test_alert",
        title="Test Notification from Social Media Scheduler",
        body="This is a test notification confirming that Telegram alerts are correctly configured and working.",
        dedupe_key=None,
        db=db,
    )

    if ok:
        return {"status": "success", "message": "Test alert successfully sent to Telegram."}
    else:
        return {
            "status": "failed",
            "message": "Failed to deliver test alert. Check that alerts are enabled and your bot has not been blocked.",
        }

