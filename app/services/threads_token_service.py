"""
Threads token lifecycle and account management service.
Handles:
- Initial token onboarding (short-lived to long-lived exchange)
- Fetching user identity (/me)
- Fernet encrypted storage in the accounts table
- Proactive token refresh when < 10 days remain
- Scheduled daily refresh job
"""

from datetime import datetime, timezone, timedelta
import json
import logging
from typing import Optional, Dict, Any, Tuple
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.config import settings
from app.models.account import Account
from app.services.crypto_service import encrypt_string, decrypt_string

logger = logging.getLogger(__name__)

GRAPH_HOST = "https://graph.threads.net"


def mask_secret(text: str, *secrets: Optional[str]) -> str:
    """Replaces sensitive tokens and secrets from strings to prevent accidental leaks."""
    if not text:
        return ""
    sanitized = str(text)
    for s in secrets:
        if s and s in sanitized:
            sanitized = sanitized.replace(s, "[REDACTED]")
    return sanitized


def fetch_threads_user_profile(access_token: str) -> Tuple[str, str]:
    """
    Calls GET /v1.0/me?fields=id,username to fetch user id and username.
    Returns (user_id, username).
    """
    url = f"{GRAPH_HOST}/v1.0/me"
    params = {
        "fields": "id,username",
        "access_token": access_token,
    }

    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(url, params=params)

        if resp.status_code == 200:
            data = resp.json()
            return str(data.get("id")), str(data.get("username", "Threads User"))
        else:
            err_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            err_msg = err_data.get("error", {}).get("message", f"HTTP {resp.status_code}")
            masked_err = mask_secret(err_msg, access_token)
            raise ValueError(f"Failed to fetch Threads user profile: {masked_err}")
    except httpx.RequestError as exc:
        raise ValueError(f"Network error connecting to Threads /me: {mask_secret(str(exc), access_token)}")


def exchange_or_validate_token(raw_token: str, app_secret: Optional[str]) -> Tuple[str, datetime]:
    """
    Takes the configured access token. If it is short-lived and app_secret is provided,
    exchanges it for a 60-day long-lived token. If already long-lived, keeps it.
    Returns (long_lived_token, expiry_datetime).
    """
    now = datetime.now(timezone.utc)

    # If app_secret is provided, attempt short-to-long token exchange
    if app_secret and app_secret.strip():
        exchange_url = f"{GRAPH_HOST}/access_token"
        params = {
            "grant_type": "th_exchange_token",
            "client_secret": app_secret.strip(),
            "access_token": raw_token.strip(),
        }

        try:
            with httpx.Client(timeout=20.0) as client:
                resp = client.get(exchange_url, params=params)

            if resp.status_code == 200:
                data = resp.json()
                new_token = data.get("access_token")
                expires_in = data.get("expires_in", 5184000)  # ~60 days in seconds
                expiry_dt = now + timedelta(seconds=expires_in)
                logger.info("Successfully exchanged short-lived Threads token for 60-day long-lived token.")
                return new_token, expiry_dt
            else:
                # If exchange failed because it's already a long-lived token or other reason,
                # we fall back to using the raw token with a default 60-day expiry
                logger.info("Token exchange not applicable or returned error; treating token as existing long-lived token.")
        except Exception as exc:
            logger.warning(f"Token exchange check exception: {mask_secret(str(exc), raw_token, app_secret)}")

    # Default to 60 days from now for pre-existing long-lived token
    return raw_token.strip(), now + timedelta(days=60)


def refresh_long_lived_token(current_token: str) -> Tuple[str, datetime]:
    """
    Refreshes an unexpired long-lived token using GET /refresh_access_token.
    Returns (refreshed_token, new_expiry_datetime).
    """
    now = datetime.now(timezone.utc)
    url = f"{GRAPH_HOST}/refresh_access_token"
    params = {
        "grant_type": "th_refresh_token",
        "access_token": current_token.strip(),
    }

    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(url, params=params)

        if resp.status_code == 200:
            data = resp.json()
            refreshed_token = data.get("access_token")
            expires_in = data.get("expires_in", 5184000)
            new_expiry = now + timedelta(seconds=expires_in)
            logger.info("Successfully refreshed long-lived Threads token.")
            return refreshed_token, new_expiry
        else:
            err_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            err_msg = err_data.get("error", {}).get("message", f"HTTP {resp.status_code}")
            raise ValueError(f"Refresh failed: {mask_secret(err_msg, current_token)}")
    except Exception as exc:
        raise ValueError(f"Threads token refresh failed: {mask_secret(str(exc), current_token)}")


def ensure_fresh_token(db: Session) -> Dict[str, Any]:
    """
    Retrieves the Threads account credentials.
    1. If no account exists in DB, initializes it from settings.THREADS_ACCESS_TOKEN.
    2. Decrypts stored credentials using Fernet.
    3. If token expiry is within 10 days, refreshes it and updates DB.
    4. If token is expired or invalid, raises ValueError.
    Returns dict: {"access_token": ..., "user_id": ..., "username": ..., "token_expiry": ...}
    """
    now = datetime.now(timezone.utc)

    # 1. Look up existing Threads account row
    stmt = select(Account).where(Account.platform == "threads")
    account = db.scalars(stmt).first()

    if not account:
        # Check if settings has token configured
        if not settings.THREADS_ACCESS_TOKEN or not settings.THREADS_ACCESS_TOKEN.strip():
            raise ValueError("Threads access token is not configured in .env")

        raw_token = settings.THREADS_ACCESS_TOKEN.strip()
        app_secret = settings.THREADS_APP_SECRET

        # Exchange if short-lived and get expiry
        final_token, expiry_dt = exchange_or_validate_token(raw_token, app_secret)

        # Fetch Threads user ID and username
        user_id, username = fetch_threads_user_profile(final_token)

        # Encrypt credentials JSON
        creds_json = json.dumps({
            "access_token": final_token,
            "user_id": user_id,
            "username": username,
        })
        encrypted_creds = encrypt_string(creds_json)

        account = Account(
            platform="threads",
            name=username,
            encrypted_credentials=encrypted_creds,
            token_expiry=expiry_dt,
        )
        db.add(account)
        db.commit()
        db.refresh(account)

    # 2. Decrypt credentials
    try:
        decrypted_json = decrypt_string(account.encrypted_credentials)
        creds = json.loads(decrypted_json)
    except Exception as exc:
        raise ValueError(f"Failed to decrypt Threads credentials: {exc}")

    current_token = creds.get("access_token")
    user_id = creds.get("user_id")
    username = creds.get("username", account.name)
    token_expiry = account.token_expiry

    if token_expiry and token_expiry.tzinfo is None:
        token_expiry = token_expiry.replace(tzinfo=timezone.utc)

    # 3. Check if expired
    if token_expiry and token_expiry <= now:
        raise ValueError(
            "Threads token expired. Generate a new token and update the Threads account."
        )

    # 4. Check if refresh needed (less than 10 days remain)
    if token_expiry and (token_expiry - now) < timedelta(days=10):
        logger.info(f"Threads token expires in {(token_expiry - now).days} days (< 10 days). Refreshing now...")
        try:
            new_token, new_expiry = refresh_long_lived_token(current_token)
            creds["access_token"] = new_token
            account.encrypted_credentials = encrypt_string(json.dumps(creds))
            account.token_expiry = new_expiry
            db.commit()
            db.refresh(account)
            current_token = new_token
            token_expiry = new_expiry
        except Exception as exc:
            logger.error(f"Automatic Threads token refresh attempt failed: {exc}")
            # If token hasn't completely expired yet, allow continuing with current token
            if token_expiry <= now:
                raise ValueError(
                    "Threads token expired. Generate a new token and update the Threads account."
                )

    return {
        "access_token": current_token,
        "user_id": user_id,
        "username": username,
        "token_expiry": token_expiry,
    }


def reload_threads_token(db: Session) -> Dict[str, Any]:
    """
    Development utility to re-read THREADS_ACCESS_TOKEN from settings / .env,
    exchange it for a 60-day long-lived token if needed, and OVERWRITE the
    encrypted token stored on the Threads account row with its refreshed expiry.
    Returns only username and expiry date, NEVER the token.
    """
    from dotenv import dotenv_values
    # Re-read .env file directly to capture newly written tokens without restarting
    vals = dotenv_values(".env")
    raw_token = vals.get("THREADS_ACCESS_TOKEN") or settings.THREADS_ACCESS_TOKEN

    if not raw_token or not raw_token.strip():
        raise ValueError("Threads access token is not configured in .env (THREADS_ACCESS_TOKEN)")

    clean_token = raw_token.strip()
    app_secret = vals.get("THREADS_APP_SECRET") or settings.THREADS_APP_SECRET

    # Exchange for long-lived token if applicable
    final_token, expiry_dt = exchange_or_validate_token(clean_token, app_secret)

    # Fetch Threads user ID and username
    user_id, username = fetch_threads_user_profile(final_token)

    # Encrypt credentials JSON
    creds_json = json.dumps({
        "access_token": final_token,
        "user_id": user_id,
        "username": username,
    })
    encrypted_creds = encrypt_string(creds_json)

    # Overwrite existing Threads account row or create if absent
    stmt = select(Account).where(Account.platform == "threads")
    account = db.scalars(stmt).first()

    if account:
        account.name = username
        account.encrypted_credentials = encrypted_creds
        account.token_expiry = expiry_dt
    else:
        account = Account(
            platform="threads",
            name=username,
            encrypted_credentials=encrypted_creds,
            token_expiry=expiry_dt,
        )
        db.add(account)

    db.commit()
    db.refresh(account)

    logger.info(f"Threads credentials successfully reloaded and encrypted for user {username}.")
    return {
        "username": username,
        "token_expiry": expiry_dt,
    }

