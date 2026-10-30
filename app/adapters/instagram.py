"""
Instagram platform adapter.
Publishes single image and video/reels posts to Instagram Business/Creator accounts
using the official Meta Instagram Graph API (v19.0).
API Reference: https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/content-publishing
"""

import logging
import time
from typing import Optional, Any, Dict
import httpx
from sqlalchemy.orm import Session, object_session
from app.config import settings
from app.database import SessionLocal
from app.adapters.base import BaseAdapter, PublishResult
from app.services.threads_token_service import mask_secret
from app.services.instagram_token_service import (
    ensure_fresh_instagram_token,
    GRAPH_HOST,
)

logger = logging.getLogger(__name__)

INSTAGRAM_MAX_CAPTION_LENGTH = 2200


class InstagramAdapter(BaseAdapter):
    """
    Adapter that publishes images and video/reels to Instagram via Meta Graph API.
    Flow:
    1. Create media container: POST /{ig-user-id}/media
    2. Poll container status: GET /{container-id}?fields=status_code,status
    3. Publish container: POST /{ig-user-id}/media_publish
    """

    def __init__(self, client: Optional[httpx.Client] = None):
        self.client = client

    def _execute_publish(
        self,
        post: Any,
        target: Any,
        db: Session,
    ) -> PublishResult:
        # 1. Retrieve authenticated Instagram account credentials
        try:
            creds = ensure_fresh_instagram_token(db)
        except Exception as exc:
            err_msg = str(exc)
            logger.error(f"Instagram credentials error: {err_msg}")
            return PublishResult(
                success=False,
                error_message=err_msg,
                retryable=False,
            )

        access_token = creds["access_token"]
        account_id = creds["account_id"]
        app_secret = settings.INSTAGRAM_APP_SECRET

        if not account_id:
            return PublishResult(
                success=False,
                error_message="Instagram Business Account ID could not be found. Check your linked Facebook Page or set INSTAGRAM_ACCOUNT_ID in .env.",
                retryable=False,
            )

        # 2. Extract content (title + body) and build caption
        title = (
            getattr(post, "title", None)
            if hasattr(post, "title")
            else (post.get("title") if isinstance(post, dict) else None)
        )
        body = (
            getattr(post, "body", "")
            if hasattr(post, "body")
            else (post.get("body", "") if isinstance(post, dict) else str(post))
        )

        caption = f"{title.strip()}\n\n{body.strip()}" if (title and title.strip()) else body.strip()

        # Enforce Instagram 2,200 character caption limit
        if len(caption) > INSTAGRAM_MAX_CAPTION_LENGTH:
            return PublishResult(
                success=False,
                error_message=f"Caption exceeds Instagram's 2,200 character limit ({len(caption)} characters).",
                retryable=False,
            )

        # 3. Extract media URL (Required: Instagram does not allow text-only posts)
        media_url = (
            getattr(post, "media_url", None)
            or (post.get("media_url") if isinstance(post, dict) else None)
            or getattr(post, "media_path", None)
            or (post.get("media_path") if isinstance(post, dict) else None)
        )

        if not media_url or not str(media_url).strip():
            return PublishResult(
                success=False,
                error_message="Instagram requires a public image or video URL. Text-only posts are not supported by the Instagram Graph API.",
                retryable=False,
            )

        clean_media_url = str(media_url).strip()
        if not (clean_media_url.startswith("http://") or clean_media_url.startswith("https://")):
            return PublishResult(
                success=False,
                error_message=f"Instagram media URL must be a publicly accessible HTTP/HTTPS URL (got: '{clean_media_url[:30]}...').",
                retryable=False,
            )

        # Use injected client if provided (for tests), else create a new one
        def get_client():
            if self.client is not None:
                return self.client
            return httpx.Client(timeout=25.0)

        client_ctx = get_client()

        try:
            # Step A: Create Media Container POST /{ig-user-id}/media
            container_url = f"{GRAPH_HOST}/{account_id}/media"
            is_video = any(
                clean_media_url.lower().split("?")[0].endswith(ext)
                for ext in (".mp4", ".mov", ".m4v")
            )

            payload: Dict[str, Any] = {
                "caption": caption,
                "access_token": access_token,
            }

            if is_video:
                payload["media_type"] = "REELS"
                payload["video_url"] = clean_media_url
            else:
                payload["image_url"] = clean_media_url

            resp_create = client_ctx.post(container_url, data=payload)
            if resp_create.status_code != 200:
                code = resp_create.status_code
                err_msg = f"HTTP {code}"
                try:
                    err_json = resp_create.json()
                    err_msg = err_json.get("error", {}).get("message", err_msg)
                except Exception:
                    pass

                sanitized = mask_secret(err_msg, access_token, app_secret)
                is_retryable = code not in (400, 401, 403, 404)
                return PublishResult(
                    success=False,
                    error_message=f"Instagram container creation failed ({code}): {sanitized}",
                    retryable=is_retryable,
                )

            container_id = resp_create.json().get("id")
            if not container_id:
                return PublishResult(
                    success=False,
                    error_message="Instagram API did not return a container ID.",
                    retryable=True,
                )

            # Step B: Poll Container Status GET /{container-id}?fields=status_code,status
            status_url = f"{GRAPH_HOST}/{container_id}"
            status_params = {
                "fields": "status_code,status",
                "access_token": access_token,
            }
            poll_start = time.time()
            container_ready = False

            while time.time() - poll_start < 45.0:
                resp_poll = client_ctx.get(status_url, params=status_params)
                if resp_poll.status_code == 200:
                    poll_data = resp_poll.json()
                    status_code_val = poll_data.get("status_code", "").upper()

                    if status_code_val in ("FINISHED", "PUBLISHED"):
                        container_ready = True
                        break
                    elif status_code_val == "ERROR":
                        raw_err = poll_data.get("status", "Media processing failed with ERROR")
                        sanitized = mask_secret(raw_err, access_token, app_secret)
                        return PublishResult(
                            success=False,
                            error_message=f"Instagram container error: {sanitized}",
                            retryable=False,
                        )
                    elif status_code_val == "EXPIRED":
                        return PublishResult(
                            success=False,
                            error_message="Instagram container expired before publication.",
                            retryable=False,
                        )
                elif resp_poll.status_code in (400, 401, 403, 404):
                    return PublishResult(
                        success=False,
                        error_message=f"Instagram container check failed (HTTP {resp_poll.status_code})",
                        retryable=False,
                    )

                time.sleep(1.5)

            if not container_ready:
                return PublishResult(
                    success=False,
                    error_message="Instagram media container processing timed out after 45 seconds.",
                    retryable=True,
                )

            # Step C: Publish Media Container POST /{ig-user-id}/media_publish
            publish_url = f"{GRAPH_HOST}/{account_id}/media_publish"
            publish_payload = {
                "creation_id": container_id,
                "access_token": access_token,
            }
            resp_publish = client_ctx.post(publish_url, data=publish_payload)

            if resp_publish.status_code != 200:
                code = resp_publish.status_code
                err_msg = f"HTTP {code}"
                try:
                    err_json = resp_publish.json()
                    err_msg = err_json.get("error", {}).get("message", err_msg)
                except Exception:
                    pass

                sanitized = mask_secret(err_msg, access_token, app_secret)
                is_retryable = code not in (400, 401, 403, 404)
                return PublishResult(
                    success=False,
                    error_message=f"Instagram publish failed ({code}): {sanitized}",
                    retryable=is_retryable,
                )

            media_id = str(resp_publish.json().get("id", "instagram_post"))
            return PublishResult(
                success=True,
                external_post_id=media_id,
                retryable=True,
            )

        except httpx.TimeoutException:
            return PublishResult(
                success=False,
                error_message="Instagram API request timed out.",
                retryable=True,
            )
        except httpx.RequestError as exc:
            sanitized = mask_secret(str(exc), access_token, app_secret)
            return PublishResult(
                success=False,
                error_message=f"Network error connecting to Instagram Graph API: {sanitized}",
                retryable=True,
            )
        except Exception as exc:
            sanitized = mask_secret(str(exc), access_token, app_secret)
            return PublishResult(
                success=False,
                error_message=f"Unexpected Instagram error: {sanitized}",
                retryable=True,
            )
        finally:
            if self.client is None and hasattr(client_ctx, "close"):
                client_ctx.close()

    def publish(self, post: Any, target: Any = None) -> PublishResult:
        """BaseAdapter publish entrypoint managing database session."""
        existing_session = object_session(target) if target is not None else None
        if existing_session is not None:
            return self._execute_publish(post, target, existing_session)

        with SessionLocal() as db:
            return self._execute_publish(post, target, db)

