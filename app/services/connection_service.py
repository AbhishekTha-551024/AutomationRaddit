"""
Service for platform credential management, connection diagnostic testing,
read-only health checks, and secure credential storage.
Ensures zero secret leakage and safe operational status reporting.
"""

from datetime import datetime, timezone, timedelta
import json
import logging
from typing import Optional, Dict, Any, List, Tuple
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.config import settings
from app.models.account import Account
from app.models.connection import ConnectionStatus
from app.models.log import Log
from app.models.target import PostTarget, TargetStatus
from app.services.crypto_service import encrypt_string, decrypt_string
from app.services.threads_token_service import mask_secret

logger = logging.getLogger(__name__)

GRAPH_HOST_THREADS = "https://graph.threads.net"
GRAPH_HOST_FB = "https://graph.facebook.com/v19.0"


def mask_credential_value(val: Optional[str]) -> str:
    """Returns a safe masked version of a credential (e.g. ••••1234), never the secret itself."""
    if not val or not str(val).strip():
        return "Not configured"
    s = str(val).strip()
    if len(s) <= 4:
        return "••••"
    return f"••••{s[-4:]}"


def get_platform_credentials(platform: str, db: Optional[Session] = None) -> Dict[str, Any]:
    """
    Retrieves credentials for a given platform.
    Prioritizes encrypted credentials stored in the `accounts` table first,
    and falls back to application `.env` settings if no account row exists.
    """
    normalized = platform.strip().lower()

    # 1. Check database accounts table if db is provided or available
    if db is not None:
        try:
            stmt = select(Account).where(Account.platform == normalized).order_by(Account.id.desc())
            account = db.scalars(stmt).first()
            if account and account.encrypted_credentials:
                try:
                    decrypted_raw = decrypt_string(account.encrypted_credentials)
                    parsed = json.loads(decrypted_raw)
                    parsed["source"] = "db"
                    parsed["account_id"] = account.id
                    parsed["name"] = account.name
                    parsed["token_expiry"] = account.token_expiry
                    return parsed
                except Exception as dec_err:
                    logger.warning(f"Failed to decrypt credentials for {normalized}: {dec_err}")
        except Exception as q_err:
            logger.warning(f"Database query error in get_platform_credentials: {q_err}")

    # 2. Fall back to settings (.env)
    if normalized == "telegram":
        return {
            "bot_token": settings.TELEGRAM_BOT_TOKEN,
            "chat_id": settings.TELEGRAM_CHAT_ID,
            "source": "env",
            "account_id": None,
            "name": "Telegram Bot",
            "token_expiry": None,
        }
    elif normalized == "discord":
        return {
            "webhook_url": settings.DISCORD_WEBHOOK_URL,
            "source": "env",
            "account_id": None,
            "name": "Discord Webhook",
            "token_expiry": None,
        }
    elif normalized == "threads":
        return {
            "access_token": settings.THREADS_ACCESS_TOKEN,
            "app_secret": settings.THREADS_APP_SECRET,
            "source": "env",
            "account_id": None,
            "name": "Threads Account",
            "token_expiry": None,
        }
    elif normalized == "instagram":
        return {
            "access_token": settings.INSTAGRAM_ACCESS_TOKEN,
            "app_secret": settings.INSTAGRAM_APP_SECRET,
            "account_id_val": settings.INSTAGRAM_ACCOUNT_ID,
            "source": "env",
            "account_id": None,
            "name": "Instagram Account",
            "token_expiry": None,
        }

    return {"source": "none"}


def test_telegram_connection(
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Performs a safe, read-only check on Telegram credentials:
    1. Calls GET /getMe to verify bot token.
    2. Calls POST /getChat to verify chat ID exists and retrieve chat title.
    3. Calls POST /getChatMember to verify bot permissions.
    Never sends a message or leaks tokens.
    """
    token = (bot_token or "").strip()
    target_chat = (chat_id or "").strip()

    if not token or not target_chat:
        return {
            "ok": False,
            "message": "Telegram Bot Token or Chat ID is missing.",
            "suggestion": "Configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in Connections or .env.",
            "identity": None,
        }

    # 1. Test bot token with /getMe
    get_me_url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp_me = client.get(get_me_url)
            data_me = resp_me.json()

        if resp_me.status_code != 200 or not data_me.get("ok"):
            desc = data_me.get("description", "Unauthorized")
            return {
                "ok": False,
                "message": f"Telegram bot token rejected ({desc}).",
                "suggestion": "Check that the bot token is active and copied properly from @BotFather.",
                "identity": None,
            }

        bot_info = data_me.get("result", {})
        bot_id = bot_info.get("id")
        bot_username = bot_info.get("username", "Bot")

        # 2. Test chat id with /getChat
        get_chat_url = f"https://api.telegram.org/bot{token}/getChat"
        with httpx.Client(timeout=10.0) as client:
            resp_chat = client.post(get_chat_url, json={"chat_id": target_chat})
            data_chat = resp_chat.json()

        if resp_chat.status_code != 200 or not data_chat.get("ok"):
            desc = data_chat.get("description", "Chat not found")
            return {
                "ok": False,
                "message": f"Telegram chat could not be accessed ({desc}).",
                "suggestion": "Ensure the bot has been added to the channel/group and TELEGRAM_CHAT_ID is correct (channel IDs usually start with -100).",
                "identity": f"@{bot_username}",
            }

        chat_info = data_chat.get("result", {})
        chat_title = chat_info.get("title") or chat_info.get("username") or f"Chat {target_chat}"
        chat_type = chat_info.get("type", "chat")

        # 3. Check posting permissions with /getChatMember if in channel/supergroup
        if chat_type in ("channel", "supergroup"):
            get_member_url = f"https://api.telegram.org/bot{token}/getChatMember"
            with httpx.Client(timeout=10.0) as client:
                resp_member = client.post(get_member_url, json={"chat_id": target_chat, "user_id": bot_id})
                data_member = resp_member.json()

            if resp_member.status_code == 200 and data_member.get("ok"):
                member_info = data_member.get("result", {})
                status = member_info.get("status")
                if status == "administrator":
                    can_post = member_info.get("can_post_messages", True)
                    if not can_post and chat_type == "channel":
                        return {
                            "ok": False,
                            "message": f"Bot is an administrator in '{chat_title}' but lacks 'Post messages' permission.",
                            "suggestion": "Open Channel Settings -> Administrators -> Edit Bot -> enable 'Post messages'.",
                            "identity": f"@{bot_username} → {chat_title}",
                        }
                elif status in ("left", "kicked"):
                    return {
                        "ok": False,
                        "message": f"Bot is not a member of channel '{chat_title}'.",
                        "suggestion": "Add the bot as an administrator to your channel.",
                        "identity": f"@{bot_username}",
                    }

        return {
            "ok": True,
            "message": f"Connected to @{bot_username} posting to {chat_title} ({chat_type}).",
            "suggestion": None,
            "identity": f"@{bot_username} → {chat_title}",
        }

    except httpx.RequestError as exc:
        return {
            "ok": False,
            "message": f"Network error connecting to Telegram Bot API: {mask_secret(str(exc), token)}",
            "suggestion": "Check your internet connection and proxy settings.",
            "identity": None,
        }


def test_discord_connection(webhook_url: Optional[str] = None) -> Dict[str, Any]:
    """
    Performs a safe, read-only check on a Discord Incoming Webhook:
    Calls HTTP GET on the webhook URL (returns webhook info without posting any message).
    Never leaks webhook URL.
    """
    url = (webhook_url or "").strip()
    if not url:
        return {
            "ok": False,
            "message": "Discord Webhook URL is not configured.",
            "suggestion": "Create an Incoming Webhook in your Discord Channel Settings -> Integrations and paste the URL.",
            "identity": None,
        }

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url)

        if resp.status_code == 200:
            data = resp.json()
            webhook_name = data.get("name", "Discord Webhook")
            channel_id = data.get("channel_id")
            identity_str = f"{webhook_name} (Channel #{channel_id})" if channel_id else webhook_name
            return {
                "ok": True,
                "message": f"Webhook '{webhook_name}' is valid and ready.",
                "suggestion": None,
                "identity": identity_str,
            }
        elif resp.status_code == 404:
            return {
                "ok": False,
                "message": "Discord webhook was deleted or does not exist (HTTP 404).",
                "suggestion": "The webhook URL is invalid or was deleted in Discord. Generate a new webhook URL in Discord Channel Settings.",
                "identity": None,
            }
        elif resp.status_code in (401, 403):
            return {
                "ok": False,
                "message": "Discord webhook rejected authentication (HTTP 401/403).",
                "suggestion": "Check that the webhook URL token is intact and wasn't truncated.",
                "identity": None,
            }
        else:
            return {
                "ok": False,
                "message": f"Discord returned unexpected status HTTP {resp.status_code}.",
                "suggestion": "Verify that the Discord server is operational.",
                "identity": None,
            }

    except httpx.RequestError as exc:
        return {
            "ok": False,
            "message": f"Network error connecting to Discord: {mask_secret(str(exc), url)}",
            "suggestion": "Check your network connection to discord.com.",
            "identity": None,
        }


def test_threads_connection(access_token: Optional[str] = None) -> Dict[str, Any]:
    """
    Performs a safe read-only check on Threads credentials:
    Calls GET /v1.0/me?fields=id,username to verify authentication.
    """
    token = (access_token or "").strip()
    if not token:
        return {
            "ok": False,
            "message": "Threads access token is not configured.",
            "suggestion": "Generate a Threads User Token with threads_basic, threads_content_publish, and threads_manage_insights permissions.",
            "identity": None,
        }

    url = f"{GRAPH_HOST_THREADS}/v1.0/me"
    params = {
        "fields": "id,username",
        "access_token": token,
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, params=params)

        if resp.status_code == 200:
            data = resp.json()
            username = data.get("username", "Threads User")
            return {
                "ok": True,
                "message": f"Connected as @{username} via Threads Graph API.",
                "suggestion": None,
                "identity": f"@{username}",
            }
        else:
            err_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            err_obj = err_data.get("error", {})
            err_msg = err_obj.get("message", f"HTTP {resp.status_code}")
            err_code = err_obj.get("code")

            if "threads_manage_insights" in err_msg or "permission" in err_msg.lower():
                suggestion = "Your token is missing permissions. Add threads_content_publish, threads_basic, and threads_manage_insights."
            elif "expired" in err_msg.lower() or "session" in err_msg.lower() or err_code == 190:
                suggestion = "Token has expired. Generate a new token in Meta App Dashboard and click 'Update credentials'."
            else:
                suggestion = "Verify your Threads App configuration in Meta Developer Portal."

            return {
                "ok": False,
                "message": f"Threads authentication failed: {mask_secret(err_msg, token)}",
                "suggestion": suggestion,
                "identity": None,
            }

    except httpx.RequestError as exc:
        return {
            "ok": False,
            "message": f"Network error contacting Threads Graph API: {mask_secret(str(exc), token)}",
            "suggestion": "Check internet connectivity to graph.threads.net.",
            "identity": None,
        }


def test_instagram_connection(
    access_token: Optional[str] = None,
    account_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Performs a safe read-only check on Instagram credentials:
    Calls Facebook Graph API /me to verify token and permissions,
    and inspects connected Instagram Professional accounts.
    """
    token = (access_token or "").strip()
    if not token:
        return {
            "ok": False,
            "message": "Instagram access token is not configured.",
            "suggestion": "Configure INSTAGRAM_ACCESS_TOKEN and link your Instagram Business/Creator account to a Facebook Page.",
            "identity": None,
        }

    url = f"{GRAPH_HOST_FB}/me"
    params = {
        "fields": "id,name,accounts{id,name,instagram_business_account{id,username}}",
        "access_token": token,
    }

    try:
        with httpx.Client(timeout=12.0) as client:
            resp = client.get(url, params=params)

        if resp.status_code == 200:
            data = resp.json()
            fb_name = data.get("name", "User")
            accounts_data = data.get("accounts", {}).get("data", [])

            ig_username = None
            ig_id = None
            for page in accounts_data:
                ig_account = page.get("instagram_business_account")
                if ig_account:
                    ig_username = ig_account.get("username")
                    ig_id = ig_account.get("id")
                    break

            if ig_username:
                identity_str = f"@{ig_username} (ID: {ig_id})"
                return {
                    "ok": True,
                    "message": f"Connected as @{ig_username} (linked to Page '{accounts_data[0].get('name', 'FB Page')}').",
                    "suggestion": None,
                    "identity": identity_str,
                }
            elif account_id:
                return {
                    "ok": True,
                    "message": f"Token valid for {fb_name} with Instagram Account ID {account_id}.",
                    "suggestion": None,
                    "identity": f"Account ID: {account_id}",
                }
            else:
                return {
                    "ok": False,
                    "message": f"Token is valid for Facebook user '{fb_name}', but no linked Instagram Professional account was found.",
                    "suggestion": "Connect your Instagram Professional account to a Facebook Page in Facebook Page Settings -> Linked Accounts.",
                    "identity": fb_name,
                }
        else:
            err_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            err_msg = err_data.get("error", {}).get("message", f"HTTP {resp.status_code}")
            return {
                "ok": False,
                "message": f"Instagram/Facebook authentication failed: {mask_secret(err_msg, token)}",
                "suggestion": "Check token permissions (instagram_basic, pages_show_list, instagram_content_publish) or generate a new token.",
                "identity": None,
            }

    except httpx.RequestError as exc:
        return {
            "ok": False,
            "message": f"Network error contacting Facebook Graph API: {mask_secret(str(exc), token)}",
            "suggestion": "Check internet connectivity to graph.facebook.com.",
            "identity": None,
        }


def test_platform_connection(
    platform: str,
    db: Session,
    creds_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Executes a read-only test for the requested platform.
    If creds_override is provided, tests those credentials; otherwise tests active credentials.
    Updates the connection_status table with the test outcome.
    """
    normalized = platform.strip().lower()
    creds = creds_override or get_platform_credentials(normalized, db)

    result: Dict[str, Any]
    if normalized == "telegram":
        result = test_telegram_connection(
            bot_token=creds.get("bot_token"),
            chat_id=creds.get("chat_id"),
        )
    elif normalized == "discord":
        result = test_discord_connection(
            webhook_url=creds.get("webhook_url"),
        )
    elif normalized == "threads":
        result = test_threads_connection(
            access_token=creds.get("access_token"),
        )
    elif normalized == "instagram":
        result = test_instagram_connection(
            access_token=creds.get("access_token"),
            account_id=creds.get("account_id_val") or creds.get("account_id"),
        )
    else:
        result = {
            "ok": False,
            "message": f"Platform '{platform}' is not supported.",
            "suggestion": "Supported platforms are: telegram, discord, threads, instagram.",
            "identity": None,
        }

    # Persist or update connection_status record
    now_utc = datetime.now(timezone.utc)
    stmt = select(ConnectionStatus).where(ConnectionStatus.platform == normalized)
    conn_status = db.scalars(stmt).first()
    if not conn_status:
        conn_status = ConnectionStatus(platform=normalized)
        db.add(conn_status)

    conn_status.last_test_at = now_utc
    conn_status.last_test_ok = result["ok"]
    conn_status.last_test_message = result["message"]
    if result.get("identity"):
        conn_status.account_identity = result["identity"]
    if result["ok"]:
        conn_status.last_success_at = now_utc

    db.commit()
    db.refresh(conn_status)

    return result


def save_platform_credentials(
    db: Session,
    platform: str,
    raw_creds: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Validates new credentials with a read-only test FIRST:
    - If the test fails: keeps existing credentials, returns failure reason without changing anything.
    - If the test passes: encrypts credentials with Fernet and writes them to the `accounts` table,
      updates `connection_status`, and writes a sanitized audit Log row (no secrets).
    """
    normalized = platform.strip().lower()

    # 1. Run safe test with candidate credentials first
    test_result = test_platform_connection(normalized, db, creds_override=raw_creds)
    if not test_result["ok"]:
        return {
            "ok": False,
            "message": test_result["message"],
            "suggestion": test_result.get("suggestion"),
        }

    # 2. Candidate credentials passed. Encrypt and persist into accounts table
    now_utc = datetime.now(timezone.utc)
    clean_creds = {k: v.strip() if isinstance(v, str) else v for k, v in raw_creds.items() if v is not None}
    encrypted_cipher = encrypt_string(json.dumps(clean_creds))

    # Look up existing account or create new
    stmt = select(Account).where(Account.platform == normalized).order_by(Account.id.desc())
    account = db.scalars(stmt).first()

    identity_label = test_result.get("identity") or f"{normalized.capitalize()} Account"
    expiry_dt: Optional[datetime] = None

    # Handle token expiry for Threads/Instagram if applicable
    if normalized == "threads":
        expiry_dt = now_utc + timedelta(days=60)
    elif normalized == "instagram":
        expiry_dt = now_utc + timedelta(days=60)

    if account:
        account.name = identity_label
        account.encrypted_credentials = encrypted_cipher
        if expiry_dt:
            account.token_expiry = expiry_dt
    else:
        account = Account(
            platform=normalized,
            name=identity_label,
            encrypted_credentials=encrypted_cipher,
            token_expiry=expiry_dt,
        )
        db.add(account)

    db.flush()

    # 3. Update connection_status table
    stmt_status = select(ConnectionStatus).where(ConnectionStatus.platform == normalized)
    conn_status = db.scalars(stmt_status).first()
    if not conn_status:
        conn_status = ConnectionStatus(platform=normalized)
        db.add(conn_status)

    conn_status.account_id = account.id
    conn_status.last_test_at = now_utc
    conn_status.last_test_ok = True
    conn_status.last_test_message = test_result["message"]
    conn_status.account_identity = identity_label
    conn_status.last_success_at = now_utc

    # 4. Write sanitized audit log (Strictly no values/secrets)
    db.add(Log(
        post_target_id=None,
        level="INFO",
        message=f"Credentials updated and verified for platform '{normalized}'.",
    ))

    db.commit()
    logger.info(f"Platform credentials updated for '{normalized}'.")

    return {
        "ok": True,
        "message": f"Connection verified and credentials saved for {normalized.capitalize()}.",
        "identity": identity_label,
    }


def get_all_connections_summary(db: Session) -> List[Dict[str, Any]]:
    """
    Assembles high-level operational status and health cards for all 4 platforms:
    Telegram, Discord, Threads, Instagram.
    Calculates state (Connected, Needs attention, Not connected, Expired),
    identity, expiry, last success, last error, pending & failed target counts.
    """
    now_utc = datetime.now(timezone.utc)
    platforms = ["telegram", "discord", "threads", "instagram"]
    summary_list: List[Dict[str, Any]] = []

    for plat in platforms:
        creds = get_platform_credentials(plat, db)
        has_credentials = creds.get("source") != "none" and any(
            v for k, v in creds.items() if k not in ("source", "account_id", "name", "token_expiry")
        )

        # Query ConnectionStatus
        status_stmt = select(ConnectionStatus).where(ConnectionStatus.platform == plat)
        conn_status = db.scalars(status_stmt).first()

        # Query target counts (pending & failed)
        targets_stmt = select(PostTarget).where(
            PostTarget.platform == plat,
            PostTarget.deleted_at.is_(None),
        )
        all_targets = db.scalars(targets_stmt).all()
        pending_count = sum(1 for t in all_targets if t.status == TargetStatus.pending)
        failed_count = sum(1 for t in all_targets if t.status == TargetStatus.failed)

        # Last successful post
        posted_targets = [t for t in all_targets if t.status == TargetStatus.posted and t.posted_at]
        last_posted_dt = max((t.posted_at for t in posted_targets), default=None)

        # Most recent failure message
        failed_targets = [t for t in all_targets if t.status == TargetStatus.failed and t.error_message]
        last_failure_msg = failed_targets[-1].error_message if failed_targets else None
        if last_failure_msg and len(last_failure_msg) > 100:
            last_failure_msg = last_failure_msg[:97] + "..."

        token_expiry = creds.get("token_expiry")
        expires_in_days = None
        is_expired = False
        expires_soon = False

        if token_expiry:
            # Ensure timezone-aware UTC
            if token_expiry.tzinfo is None:
                token_expiry = token_expiry.replace(tzinfo=timezone.utc)
            delta = token_expiry - now_utc
            expires_in_days = delta.days
            if delta.total_seconds() <= 0:
                is_expired = True
            elif expires_in_days <= 7:
                expires_soon = True

        # Determine Connection State
        # States: "Connected", "Needs attention", "Not connected", "Expired"
        state = "Not connected"
        state_variant = "secondary"

        if not has_credentials:
            state = "Not connected"
            state_variant = "secondary"
        elif is_expired:
            state = "Expired"
            state_variant = "danger"
        elif conn_status and not conn_status.last_test_ok:
            state = "Needs attention"
            state_variant = "warning"
        elif expires_soon:
            state = "Needs attention"
            state_variant = "warning"
        else:
            state = "Connected"
            state_variant = "success"

        identity = (
            (conn_status.account_identity if conn_status else None)
            or creds.get("name")
            or f"{plat.capitalize()} Account"
        )

        summary_list.append({
            "platform": plat,
            "platform_name": plat.capitalize(),
            "state": state,
            "state_variant": state_variant,
            "identity": identity,
            "has_credentials": has_credentials,
            "credentials_source": creds.get("source"),
            "token_expiry": token_expiry,
            "expires_in_days": expires_in_days,
            "supports_refresh": plat in ("threads", "instagram"),
            "last_test_at": conn_status.last_test_at if conn_status else None,
            "last_test_ok": conn_status.last_test_ok if conn_status else None,
            "last_test_message": conn_status.last_test_message if conn_status else None,
            "last_success_at": last_posted_dt or (conn_status.last_success_at if conn_status else None),
            "last_failure_message": last_failure_msg,
            "pending_count": pending_count,
            "failed_count": failed_count,
        })

    return summary_list


def run_all_connection_tests(db: Session) -> Dict[str, Any]:
    """
    Executes automated daily connection tests across all platforms.
    Updates connection_status table and triggers alerts if any platform fails.
    """
    from app.services.alerts import send_alert

    results: Dict[str, Any] = {}
    for plat in ["telegram", "discord", "threads", "instagram"]:
        try:
            res = test_platform_connection(plat, db)
            results[plat] = res
            if not res["ok"]:
                # Trigger alert for failed connection test
                send_alert(
                    kind="connection_failed",
                    title=f"{plat.capitalize()} connection failed",
                    body=f"{res['message']}\nSuggested fix: {res.get('suggestion') or 'Update credentials in /connections.'}",
                    dedupe_key=f"conn_failed:{plat}",
                )
        except Exception as exc:
            logger.error(f"Daily connection test error for {plat}: {exc}")
            results[plat] = {"ok": False, "message": str(exc)}

    return results

