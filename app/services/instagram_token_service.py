"""
Instagram token lifecycle and account management service.
Handles:
- Account inspection (/me, /me/accounts, /me/permissions)
- Auto-discovery of linked Instagram Business/Creator Account
- Automatic exchange of short-lived tokens for 60-day long-lived tokens
- Fernet encrypted storage in the accounts database table
- Error sanitization and secret masking
"""

import json
import logging
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timezone, timedelta
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.config import settings
from app.models.account import Account
from app.services.crypto_service import encrypt_string, decrypt_string
from app.services.threads_token_service import mask_secret

logger = logging.getLogger(__name__)

GRAPH_HOST = "https://graph.facebook.com/v19.0"


def exchange_instagram_long_lived_token(
    raw_token: str,
    app_id: Optional[str],
    app_secret: Optional[str],
) -> Tuple[str, datetime]:
    """
    Attempts to exchange a short-lived (1-hour) User token for a 60-day long-lived User token
    using GET /oauth/access_token?grant_type=fb_exchange_token.
    Returns (token, expiry_datetime).
    """
    now = datetime.now(timezone.utc)
    if app_id and app_secret and app_id.strip() and app_secret.strip():
        exchange_url = f"{GRAPH_HOST}/oauth/access_token"
        params = {
            "grant_type": "fb_exchange_token",
            "client_id": app_id.strip(),
            "client_secret": app_secret.strip(),
            "fb_exchange_token": raw_token.strip(),
        }
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(exchange_url, params=params)

            if resp.status_code == 200:
                data = resp.json()
                long_lived_token = data.get("access_token")
                expires_in = data.get("expires_in", 5184000)  # ~60 days in seconds
                logger.info("Successfully exchanged Instagram token for a 60-day long-lived token.")
                return long_lived_token, now + timedelta(seconds=expires_in)
            else:
                logger.info("Instagram token exchange returned error or not applicable; keeping provided token.")
        except Exception as exc:
            logger.warning(f"Instagram token exchange notice: {mask_secret(str(exc), raw_token, app_secret)}")

    return raw_token.strip(), now + timedelta(days=60)


def inspect_instagram_account(
    access_token: str,
    account_id_override: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Queries Meta Graph API to inspect:
    1. Facebook User profile (/me)
    2. Granted permissions (/me/permissions)
    3. Connected Facebook Pages and linked Instagram Business accounts (/me/accounts)
    Returns a comprehensive diagnostic dictionary without leaking tokens.
    """
    clean_token = access_token.strip()
    result: Dict[str, Any] = {
        "user_name": None,
        "user_id": None,
        "instagram_account_id": None,
        "instagram_username": None,
        "facebook_pages": [],
        "granted_permissions": [],
        "missing_permissions": [],
        "status": "pending",
        "guidance": None,
    }

    try:
        with httpx.Client(timeout=20.0) as client:
            # 1. Fetch user profile
            resp_me = client.get(f"{GRAPH_HOST}/me", params={"access_token": clean_token})
            if resp_me.status_code == 200:
                me_data = resp_me.json()
                result["user_name"] = me_data.get("name")
                result["user_id"] = me_data.get("id")
            else:
                err_data = resp_me.json() if resp_me.headers.get("content-type", "").startswith("application/json") else {}
                err_obj = err_data.get("error", {})
                err_msg = err_obj.get("message", f"HTTP {resp_me.status_code}")
                err_code = err_obj.get("code")
                sanitized_msg = mask_secret(err_msg, clean_token)

                if err_code == 190 or "expired" in err_msg.lower() or "session" in err_msg.lower():
                    result["status"] = "token_expired"
                    result["guidance"] = (
                        f"Instagram access token is expired ({sanitized_msg}). "
                        "Please generate a new access token in Meta Graph API Explorer and update INSTAGRAM_ACCESS_TOKEN in .env."
                    )
                    return result
                else:
                    result["status"] = "token_invalid"
                    result["guidance"] = f"Meta Graph API error validating token: {sanitized_msg}"
                    return result

            # 2. Fetch permissions
            resp_perms = client.get(f"{GRAPH_HOST}/me/permissions", params={"access_token": clean_token})
            if resp_perms.status_code == 200:
                perms_data = resp_perms.json().get("data", [])
                for p in perms_data:
                    perm_name = p.get("permission")
                    status = p.get("status")
                    if status == "granted":
                        result["granted_permissions"].append(perm_name)
                    else:
                        result["missing_permissions"].append(perm_name)

            # 3. Fetch Facebook Pages and linked Instagram Business accounts
            accounts_params = {
                "fields": "id,name,instagram_business_account{id,username,name}",
                "access_token": clean_token,
            }
            resp_acc = client.get(f"{GRAPH_HOST}/me/accounts", params=accounts_params)
            discovered_ig_id = None
            discovered_ig_user = None

            if resp_acc.status_code == 200:
                pages_data = resp_acc.json().get("data", [])
                for page in pages_data:
                    page_info = {
                        "page_id": page.get("id"),
                        "page_name": page.get("name"),
                        "instagram_business_account": None,
                    }
                    ig = page.get("instagram_business_account")
                    if ig:
                        page_info["instagram_business_account"] = {
                            "id": ig.get("id"),
                            "username": ig.get("username"),
                        }
                        if not discovered_ig_id:
                            discovered_ig_id = str(ig.get("id"))
                            discovered_ig_user = str(ig.get("username", "instagram_user"))

                    result["facebook_pages"].append(page_info)

            # 4. Determine final Instagram Account ID
            if account_id_override and account_id_override.strip():
                result["instagram_account_id"] = account_id_override.strip()
                # Try fetching username for the override account
                resp_ig = client.get(
                    f"{GRAPH_HOST}/{account_id_override.strip()}",
                    params={"fields": "id,username", "access_token": clean_token},
                )
                if resp_ig.status_code == 200:
                    result["instagram_username"] = resp_ig.json().get("username")
            elif discovered_ig_id:
                result["instagram_account_id"] = discovered_ig_id
                result["instagram_username"] = discovered_ig_user

            # 5. Provide guidance if incomplete
            if not result["instagram_account_id"]:
                if len(result["facebook_pages"]) == 0:
                    result["status"] = "needs_facebook_page"
                    result["guidance"] = (
                        "No Facebook Pages found for this user token. To publish to Instagram via Meta Graph API: "
                        "1) Create a Facebook Page, 2) Convert your Instagram to a Professional (Business/Creator) account, "
                        "3) In Facebook Page Settings -> Linked Accounts, link your Instagram account, and "
                        "4) Re-generate your access token with 'pages_show_list' and 'instagram_content_publish'."
                    )
                else:
                    page_names = [p.get("page_name") for p in result["facebook_pages"] if p.get("page_name")]
                    page_str = ", ".join(f"'{n}'" for n in page_names) if page_names else "your page"
                    result["status"] = "needs_instagram_link"
                    result["guidance"] = (
                        f"Facebook Page ({page_str}) found, but no Instagram Professional account is linked to it yet. "
                        "Go to Facebook Page Settings -> Linked Accounts -> Instagram, and click 'Connect account' to link your Instagram account."
                    )
            else:
                result["status"] = "connected"

    except Exception as exc:
        sanitized = mask_secret(str(exc), clean_token)
        result["status"] = "error"
        result["error"] = sanitized
        result["guidance"] = f"Failed to connect to Meta Graph API: {sanitized}"

    return result


def ensure_fresh_instagram_token(db: Session) -> Dict[str, Any]:
    """
    Retrieves Instagram credentials.
    1. Checks if Account row exists in DB for platform='instagram'.
    2. If not, inspects settings.INSTAGRAM_ACCESS_TOKEN and saves encrypted credentials.
    3. Decrypts credentials and returns dict:
       {"access_token": ..., "account_id": ..., "username": ...}
    Raises ValueError with a clear, sanitized message on failure.
    """
    clean_token = settings.INSTAGRAM_ACCESS_TOKEN.strip() if settings.INSTAGRAM_ACCESS_TOKEN else None
    if not clean_token:
        raise ValueError("Instagram access token is not configured in .env (INSTAGRAM_ACCESS_TOKEN)")

    account_id_override = settings.INSTAGRAM_ACCOUNT_ID.strip() if settings.INSTAGRAM_ACCOUNT_ID else None
    app_id = settings.INSTAGRAM_APP_ID
    app_secret = settings.INSTAGRAM_APP_SECRET

    # Check database for existing Instagram account
    stmt = select(Account).where(Account.platform == "instagram")
    account = db.scalars(stmt).first()

    if not account:
        # First diagnose token validity
        diag = inspect_instagram_account(clean_token, account_id_override)
        status = diag.get("status")

        if status in ("token_expired", "token_invalid", "error"):
            raise ValueError(diag.get("guidance", "Invalid or expired Instagram access token."))

        ig_id = diag.get("instagram_account_id")
        ig_username = diag.get("instagram_username") or "Instagram Business"

        if not ig_id:
            guidance = diag.get("guidance", "Could not discover Instagram Business Account ID from token.")
            raise ValueError(f"Instagram setup incomplete: {guidance}")

        # Attempt to exchange short-lived token for a 60-day token
        final_token, expiry_dt = exchange_instagram_long_lived_token(clean_token, app_id, app_secret)

        # Encrypt credentials JSON
        creds_json = json.dumps({
            "access_token": final_token,
            "account_id": ig_id,
            "username": ig_username,
        })
        encrypted_creds = encrypt_string(creds_json)

        account = Account(
            platform="instagram",
            name=ig_username,
            encrypted_credentials=encrypted_creds,
            token_expiry=expiry_dt,
        )
        db.add(account)
        db.commit()
        db.refresh(account)

    # Decrypt stored credentials
    try:
        decrypted_json = decrypt_string(account.encrypted_credentials)
        creds = json.loads(decrypted_json)
    except Exception as exc:
        raise ValueError(f"Failed to decrypt Instagram credentials: {exc}")

    # If the user updated INSTAGRAM_ACCOUNT_ID in settings, update it
    if account_id_override and creds.get("account_id") != account_id_override:
        creds["account_id"] = account_id_override
        account.encrypted_credentials = encrypt_string(json.dumps(creds))
        db.commit()

    return {
        "access_token": creds.get("access_token"),
        "account_id": creds.get("account_id"),
        "username": creds.get("username", account.name),
    }
