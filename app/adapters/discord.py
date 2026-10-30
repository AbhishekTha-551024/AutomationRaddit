"""
Discord webhook platform adapter.
Publishes posts directly to a Discord channel via Discord Incoming Webhooks.
"""

from typing import Optional, Any
import httpx
from app.config import settings
from app.adapters.base import BaseAdapter, PublishResult, DeleteResult
from app.services.threads_token_service import mask_secret


class DiscordAdapter(BaseAdapter):
    """
    Adapter that communicates with Discord via Incoming Webhooks.
    """
    supports_delete: bool = True

    def __init__(self, webhook_url: Optional[str] = None):
        # Read from accounts table first, then fall back to settings (.env)
        if webhook_url is None:
            from app.database import SessionLocal
            from app.services.connection_service import get_platform_credentials
            try:
                with SessionLocal() as db:
                    creds = get_platform_credentials("discord", db)
            except Exception:
                creds = get_platform_credentials("discord", None)
            self.webhook_url = creds.get("webhook_url")
        else:
            self.webhook_url = webhook_url

    def publish(self, post: Any, target: Any = None) -> PublishResult:
        """
        Publishes the given post to Discord via webhook.
        Truncates content to 2000 characters if needed.
        Never exposes the webhook URL or secret token in errors or logs.
        """
        # 1. Validate that the webhook URL is configured
        if not self.webhook_url or not self.webhook_url.strip():
            return PublishResult(
                success=False,
                error_message="Discord webhook URL is not configured",
                retryable=False,
            )

        # 2. Extract title and body
        title = getattr(post, "title", None) if hasattr(post, "title") else (post.get("title") if isinstance(post, dict) else None)
        body = getattr(post, "body", "") if hasattr(post, "body") else (post.get("body", "") if isinstance(post, dict) else str(post))

        if title:
            content = f"{title}\n\n{body}"
        else:
            content = body

        # 3. Discord enforces a 2000-character limit on message content
        if len(content) > 2000:
            content = content[:1990] + "..."

        # 4. Prepare webhook URL with wait=true to receive the message ID back
        separator = "&" if "?" in self.webhook_url else "?"
        query_url = f"{self.webhook_url}{separator}wait=true"

        payload = {
            "content": content,
        }

        try:
            with httpx.Client(timeout=15.0) as client:
                response = client.post(query_url, json=payload)

            if response.status_code in (200, 204):
                external_id = "discord_message"
                try:
                    data = response.json()
                    external_id = str(data.get("id", "discord_message"))
                except Exception:
                    pass

                return PublishResult(
                    success=True,
                    external_post_id=external_id,
                    retryable=True,
                )
            else:
                code = response.status_code
                description = f"HTTP {code}"
                try:
                    data = response.json()
                    description = data.get("message", description)
                except Exception:
                    pass

                # Categorize errors:
                # 400, 401, 403, 404 are permanent (invalid/deleted webhook)
                # 429 (rate limit) and 5xx are retryable
                is_retryable = code not in (400, 401, 403, 404)

                if code == 404:
                    err_msg = f"Discord webhook not found ({code})"
                else:
                    err_msg = f"Discord webhook error ({code}): {description}"

                return PublishResult(
                    success=False,
                    error_message=err_msg,
                    retryable=is_retryable,
                )

        except httpx.TimeoutException:
            return PublishResult(
                success=False,
                error_message="Discord webhook request timed out after 15 seconds.",
                retryable=True,
            )
        except httpx.RequestError as exc:
            # Mask URL from error text
            err_text = str(exc)
            if self.webhook_url in err_text:
                err_text = err_text.replace(self.webhook_url, "[REDACTED_WEBHOOK_URL]")
            return PublishResult(
                success=False,
                error_message=f"Network error connecting to Discord: {err_text}",
                retryable=True,
            )
        except Exception as exc:
            err_text = str(exc)
            if self.webhook_url and self.webhook_url in err_text:
                err_text = err_text.replace(self.webhook_url, "[REDACTED_WEBHOOK_URL]")
            return PublishResult(
                success=False,
                error_message=f"Failed to publish to Discord: {err_text}",
                retryable=True,
            )

    def delete(self, target: Any) -> DeleteResult:
        """
        Deletes a previously published message from Discord webhook.
        Queries DELETE {webhook_url}/messages/{message_id}.
        """
        if not self.webhook_url or not self.webhook_url.strip():
            return DeleteResult(success=False, error_message="Discord webhook URL not configured")

        message_id = getattr(target, "external_post_id", None)
        if not message_id or str(message_id) == "discord_message":
            return DeleteResult(success=False, error_message="No valid Discord message ID found for target")

        url = f"{self.webhook_url}/messages/{message_id}"
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.delete(url)

            if resp.status_code == 204:
                return DeleteResult(success=True)

            err_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            desc = err_data.get("message", f"HTTP {resp.status_code}")
            return DeleteResult(success=False, error_message=mask_secret(desc, self.webhook_url))

        except Exception as exc:
            return DeleteResult(success=False, error_message=mask_secret(str(exc), self.webhook_url))
