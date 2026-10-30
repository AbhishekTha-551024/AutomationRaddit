"""
Telegram platform adapter.
Publishes posts to a Telegram channel or group using the Telegram Bot API.
"""

from typing import Optional, Any
import httpx
from app.config import settings
from app.adapters.base import BaseAdapter, PublishResult, DeleteResult
from app.services.threads_token_service import mask_secret


class TelegramAdapter(BaseAdapter):
    """
    Adapter that communicates with the official Telegram Bot API.
    Sends text messages to a configured chat ID.
    """
    supports_delete: bool = True

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
    ):
        # Read from accounts table first, then fall back to settings (.env)
        if bot_token is None or chat_id is None:
            from app.database import SessionLocal
            from app.services.connection_service import get_platform_credentials
            try:
                with SessionLocal() as db:
                    creds = get_platform_credentials("telegram", db)
            except Exception:
                creds = get_platform_credentials("telegram", None)
            self.bot_token = bot_token if bot_token is not None else creds.get("bot_token")
            self.chat_id = chat_id if chat_id is not None else creds.get("chat_id")
        else:
            self.bot_token = bot_token
            self.chat_id = chat_id

    def publish(self, post: Any, target: Any = None) -> PublishResult:
        """
        Publishes the given post to Telegram via the sendMessage endpoint.

        Parameters:
        - post: An object or dict with 'title' and 'body' attributes/keys.
        - target: Optional target context (unused in simple publish).

        Returns:
        - PublishResult with success status, external_post_id on success,
          or sanitized error_message and retryable flag on failure.
        """
        # Validate that credentials are provided and non-empty
        if not self.bot_token or not self.chat_id:
            return PublishResult(
                success=False,
                error_message="Telegram credentials missing: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not configured.",
                retryable=False,  # Configuration error is permanent until user fixes it
            )

        # Extract title and body from post object or dict
        title = getattr(post, "title", None) if hasattr(post, "title") else (post.get("title") if isinstance(post, dict) else None)
        body = getattr(post, "body", "") if hasattr(post, "body") else (post.get("body", "") if isinstance(post, dict) else str(post))

        # Format message: bold title header if present
        if title:
            text = f"*{title}*\n\n{body}"
        else:
            text = body

        # Telegram Bot API URL (never logged or exposed in errors)
        api_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }

        try:
            with httpx.Client(timeout=15.0) as client:
                response = client.post(api_url, json=payload)

            # Parse Telegram JSON response
            data = response.json()

            if response.status_code == 200 and data.get("ok"):
                message_id = str(data.get("result", {}).get("message_id"))
                return PublishResult(
                    success=True,
                    external_post_id=message_id,
                    retryable=True,
                )
            else:
                # Telegram returned an error payload
                error_code = data.get("error_code", response.status_code)
                description = data.get("description", f"HTTP {response.status_code}")

                # 400 (chat not found, bad request), 401 (invalid token), 403 (bot forbidden) are permanent errors.
                # 429 (rate limit) and 5xx (server errors) are retryable.
                is_retryable = error_code not in (400, 401, 403)

                return PublishResult(
                    success=False,
                    error_message=f"Telegram API error ({error_code}): {description}",
                    retryable=is_retryable,
                )

        except httpx.TimeoutException:
            # Network timeouts are transient and retryable
            return PublishResult(
                success=False,
                error_message="Telegram API request timed out after 15 seconds.",
                retryable=True,
            )
        except httpx.RequestError as exc:
            # Network-level connection errors are transient and retryable
            err_text = str(exc)
            if self.bot_token in err_text:
                err_text = err_text.replace(self.bot_token, "[REDACTED_BOT_TOKEN]")
            return PublishResult(
                success=False,
                error_message=f"Network error connecting to Telegram API: {err_text}",
                retryable=True,
            )
        except Exception as exc:
            # Catch-all exception handler
            err_text = str(exc)
            if self.bot_token and self.bot_token in err_text:
                err_text = err_text.replace(self.bot_token, "[REDACTED_BOT_TOKEN]")
            return PublishResult(
                success=False,
                error_message=f"Failed to publish to Telegram: {err_text}",
                retryable=True,
            )

    def delete(self, target: Any) -> DeleteResult:
        """
        Deletes a previously sent message from Telegram via deleteMessage.
        """
        if not self.bot_token or not self.chat_id:
            return DeleteResult(success=False, error_message="Telegram credentials not configured")

        message_id = getattr(target, "external_post_id", None)
        if not message_id:
            return DeleteResult(success=False, error_message="No Telegram message ID found for target")

        try:
            msg_id_int = int(str(message_id).strip())
        except ValueError:
            return DeleteResult(success=False, error_message=f"Invalid Telegram message ID format: {message_id}")

        url = f"https://api.telegram.org/bot{self.bot_token}/deleteMessage"
        payload = {
            "chat_id": self.chat_id,
            "message_id": msg_id_int,
        }

        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(url, json=payload)

            if resp.status_code == 200 and resp.json().get("ok"):
                return DeleteResult(success=True)

            err_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            desc = err_data.get("description", f"HTTP {resp.status_code}")
            return DeleteResult(success=False, error_message=mask_secret(desc, self.bot_token))

        except Exception as exc:
            return DeleteResult(success=False, error_message=mask_secret(str(exc), self.bot_token))
