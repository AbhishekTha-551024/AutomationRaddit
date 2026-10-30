"""
Meta Threads platform adapter.
Publishes text posts and connected thread chains to Meta Threads using the official Threads API.
API Reference: https://graph.threads.net/v1.0
"""

import json
import logging
import re
import time
from typing import Optional, Any, List, Dict
import httpx
from sqlalchemy.orm import Session, object_session
from app.config import settings
from app.database import SessionLocal
from app.models.log import Log
from app.adapters.base import BaseAdapter, PublishResult, DeleteResult
from app.services.threads_token_service import (
    ensure_fresh_token,
    mask_secret,
    GRAPH_HOST,
)

logger = logging.getLogger(__name__)


def split_thread_parts(title: Optional[str], body: str) -> List[str]:
    """
    Splits post body by lines containing only '---' (with optional whitespace).
    Prepends title to the first part if title is present.
    Returns a list of non-empty string parts.
    """
    raw_lines = body.splitlines()
    parts: List[str] = []
    current_lines: List[str] = []

    for line in raw_lines:
        if line.strip() == "---":
            if current_lines:
                chunk = "\n".join(current_lines).strip()
                if chunk:
                    parts.append(chunk)
                current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        chunk = "\n".join(current_lines).strip()
        if chunk:
            parts.append(chunk)

    # If body was empty or only delimiters
    if not parts:
        parts = [body.strip()] if body.strip() else []

    # Prepend title to part 1 only
    if title and title.strip():
        clean_title = title.strip()
        if parts:
            parts[0] = f"{clean_title}\n\n{parts[0]}"
        else:
            parts = [clean_title]

    return parts


class ThreadsAdapter(BaseAdapter):
    """
    Publishes posts to Meta Threads via the Graph API (v1.0).
    Features:
    - 2-step publish flow: create container -> poll status -> publish.
    - Thread chains split by lines with '---'.
    - 500-char limit enforcement before any publishing.
    - Idempotent retries using target.publish_state to avoid duplicate parts.
    - Token freshness check with automatic refresh and encrypted storage.
    - Sanitized logging and error messages (never leaks secrets).
    """
    supports_delete: bool = True

    def __init__(self, client: Optional[httpx.Client] = None):
        self.client = client

    def _execute_publish(
        self,
        post: Any,
        target: Any,
        db: Session,
    ) -> PublishResult:
        # 1. Ensure token is fresh and account credentials are valid
        try:
            token_info = ensure_fresh_token(db)
        except Exception as exc:
            err_msg = str(exc)
            logger.error(f"Threads credentials error: {err_msg}")
            return PublishResult(
                success=False,
                error_message=err_msg,
                retryable=False,
            )

        access_token = token_info["access_token"]
        user_id = token_info["user_id"]
        app_secret = settings.THREADS_APP_SECRET

        # 2. Extract content
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

        media_url = (
            getattr(post, "image_url", None)
            or (post.get("image_url") if isinstance(post, dict) else None)
        )
        video_url = (
            getattr(post, "video_url", None)
            or (post.get("video_url") if isinstance(post, dict) else None)
        )
        link_attachment = (
            getattr(post, "link_attachment", None)
            or (post.get("link_attachment") if isinstance(post, dict) else None)
        )

        # 3. Split content into thread parts
        parts = split_thread_parts(title, body)
        if not parts:
            return PublishResult(
                success=False,
                error_message="Post body is empty.",
                retryable=False,
            )

        # 4. Strict 500-character validation per part BEFORE publishing any part
        for idx, part in enumerate(parts, 1):
            if len(part) > 500:
                return PublishResult(
                    success=False,
                    error_message=f"Part {idx} is {len(part)} characters, Threads allows 500",
                    retryable=False,
                )

        # 5. Check publish_state for idempotent resumption on retries
        published_part_ids: List[str] = []
        if target and getattr(target, "publish_state", None):
            try:
                state_data = json.loads(target.publish_state)
                published_part_ids = state_data.get("published_part_ids", [])
            except Exception:
                published_part_ids = []

        # If all parts were already published in a previous run
        if len(published_part_ids) >= len(parts):
            return PublishResult(
                success=True,
                external_post_id=published_part_ids[0],
                retryable=True,
            )

        start_index = len(published_part_ids)
        last_published_id: Optional[str] = (
            published_part_ids[-1] if published_part_ids else None
        )

        headers = {
            "Authorization": f"Bearer {access_token}",
        }

        # Use injected client if provided (e.g. for unit tests), else use a new client
        def get_client():
            if self.client is not None:
                return self.client
            return httpx.Client(timeout=20.0)

        client_ctx = get_client()

        try:
            for idx in range(start_index, len(parts)):
                part_text = parts[idx]

                # If this is not the first part, pause 2 seconds for Threads rate limit
                if idx > 0:
                    time.sleep(2.0)

                # Step A: Container Creation POST /{user-id}/threads
                container_url = f"{GRAPH_HOST}/v1.0/{user_id}/threads"
                payload: Dict[str, Any] = {
                    "text": part_text,
                }

                # Media handling (only on root post)
                if idx == 0 and media_url:
                    payload["media_type"] = "IMAGE"
                    payload["image_url"] = media_url
                elif idx == 0 and video_url:
                    payload["media_type"] = "VIDEO"
                    payload["video_url"] = video_url
                else:
                    payload["media_type"] = "TEXT"
                    if idx == 0 and link_attachment:
                        payload["link_attachment"] = link_attachment

                # Connected thread reply
                if last_published_id:
                    payload["reply_to_id"] = last_published_id

                resp = client_ctx.post(container_url, data=payload, headers=headers)
                if resp.status_code != 200:
                    code = resp.status_code
                    err_msg = f"HTTP {code}"
                    try:
                        err_json = resp.json()
                        err_msg = err_json.get("error", {}).get("message", err_msg)
                    except Exception:
                        pass

                    sanitized = mask_secret(err_msg, access_token, app_secret)
                    is_retryable = code not in (400, 401, 403, 404)
                    return PublishResult(
                        success=False,
                        error_message=f"Threads container creation failed ({code}): {sanitized}",
                        retryable=is_retryable,
                    )

                container_id = resp.json().get("id")
                if not container_id:
                    return PublishResult(
                        success=False,
                        error_message="Threads did not return a container ID",
                        retryable=True,
                    )

                # Step B: Poll Container Status up to 30 seconds
                status_url = f"{GRAPH_HOST}/v1.0/{container_id}"
                status_params = {"fields": "status,error_message"}
                poll_start = time.time()
                container_finished = False

                while time.time() - poll_start < 30.0:
                    status_resp = client_ctx.get(
                        status_url, params=status_params, headers=headers
                    )
                    if status_resp.status_code == 200:
                        sdata = status_resp.json()
                        status_val = sdata.get("status")
                        if status_val in ("FINISHED", "PUBLISHED"):
                            container_finished = True
                            break
                        elif status_val == "ERROR":
                            raw_err = sdata.get("error_message", "Container processing error")
                            sanitized = mask_secret(raw_err, access_token, app_secret)
                            return PublishResult(
                                success=False,
                                error_message=f"Threads container error: {sanitized}",
                                retryable=False,
                            )
                        elif status_val == "EXPIRED":
                            return PublishResult(
                                success=False,
                                error_message="Threads container expired before publication",
                                retryable=False,
                            )
                    elif status_resp.status_code in (400, 401, 403, 404):
                        return PublishResult(
                            success=False,
                            error_message=f"Threads container check error (HTTP {status_resp.status_code})",
                            retryable=False,
                        )

                    time.sleep(1.0)

                if not container_finished:
                    return PublishResult(
                        success=False,
                        error_message="Threads container processing timed out after 30 seconds",
                        retryable=True,
                    )

                # Step C: Publish Container POST /{user-id}/threads_publish
                publish_url = f"{GRAPH_HOST}/v1.0/{user_id}/threads_publish"
                publish_payload = {"creation_id": container_id}
                pub_resp = client_ctx.post(publish_url, data=publish_payload, headers=headers)

                if pub_resp.status_code != 200:
                    code = pub_resp.status_code
                    err_msg = f"HTTP {code}"
                    try:
                        err_json = pub_resp.json()
                        err_msg = err_json.get("error", {}).get("message", err_msg)
                    except Exception:
                        pass

                    sanitized = mask_secret(err_msg, access_token, app_secret)
                    is_retryable = code not in (400, 401, 403, 404)
                    return PublishResult(
                        success=False,
                        error_message=f"Threads publish failed ({code}): {sanitized}",
                        retryable=is_retryable,
                    )

                published_id = str(pub_resp.json().get("id"))
                published_part_ids.append(published_id)
                last_published_id = published_id

                # Save incremental publish state checkpoint
                if target:
                    target.publish_state = json.dumps({"published_part_ids": published_part_ids})
                    target_id = getattr(target, "id", None)
                    if target_id:
                        db.add(Log(
                            post_target_id=target_id,
                            level="INFO",
                            message=f"Threads part {idx + 1}/{len(parts)} published. Post ID: {published_id}",
                        ))
                    db.commit()

            # All parts published successfully
            root_id = published_part_ids[0] if published_part_ids else "threads_post"
            return PublishResult(
                success=True,
                external_post_id=root_id,
                retryable=True,
            )

        except httpx.TimeoutException:
            return PublishResult(
                success=False,
                error_message="Threads request timed out",
                retryable=True,
            )
        except httpx.RequestError as exc:
            sanitized = mask_secret(str(exc), access_token, app_secret)
            return PublishResult(
                success=False,
                error_message=f"Network error connecting to Threads API: {sanitized}",
                retryable=True,
            )
        except Exception as exc:
            sanitized = mask_secret(str(exc), access_token, app_secret)
            return PublishResult(
                success=False,
                error_message=f"Unexpected Threads error: {sanitized}",
                retryable=True,
            )
        finally:
            if self.client is None and hasattr(client_ctx, "close"):
                client_ctx.close()

    def publish(self, post: Any, target: Any = None) -> PublishResult:
        """
        BaseAdapter implementation.
        Manages the DB session lifecycle (reusing existing session if attached).
        """
        existing_session = object_session(target) if target is not None else None
        if existing_session is not None:
            return self._execute_publish(post, target, existing_session)

        with SessionLocal() as db:
            return self._execute_publish(post, target, db)

    def get_media_insights(self, media_id: str, db: Session) -> Dict[str, Any]:
        """
        Fetches lifetime engagement metrics for an individual Threads post.
        Queries GET /v1.0/{media_id}/insights?metric=views,likes,replies,reposts,quotes,shares.
        Raises ValueError for 400/401/403 (actionable permission/token issues).
        Raises RuntimeError for 429/5xx (temporary retryable issues).
        """
        token_info = ensure_fresh_token(db)
        access_token = token_info["access_token"]

        url = f"{GRAPH_HOST}/v1.0/{media_id}/insights"
        params = {
            "metric": "views,likes,replies,reposts,quotes,shares",
            "access_token": access_token,
        }

        client_ctx = self.client or httpx.Client(timeout=20.0)
        try:
            resp = client_ctx.get(url, params=params)

            if resp.status_code == 200:
                data = resp.json()
                metrics_data = data.get("data", [])

                # Map extracted metrics
                parsed: Dict[str, Optional[int]] = {
                    "views": None,
                    "likes": None,
                    "replies": None,
                    "reposts": None,
                    "quotes": None,
                    "shares": None,
                }

                for item in metrics_data:
                    name = item.get("name")
                    if name in parsed:
                        val = None
                        if "total_value" in item and isinstance(item["total_value"], dict):
                            val = item["total_value"].get("value")
                        elif "values" in item and isinstance(item["values"], list) and len(item["values"]) > 0:
                            val = item["values"][-1].get("value")
                        try:
                            parsed[name] = int(val) if val is not None else None
                        except (ValueError, TypeError):
                            parsed[name] = None

                # Clean raw JSON to prevent sensitive token leaks in paging URLs
                clean_payload = {k: v for k, v in data.items() if k != "paging"}
                raw_json_str = mask_secret(json.dumps(clean_payload), access_token)

                return {
                    **parsed,
                    "raw_json": raw_json_str,
                }

            elif resp.status_code in (400, 401, 403):
                raise ValueError(
                    "Missing threads_manage_insights permission or token expired. "
                    "Generate a new token and run /dev/threads-reload-token."
                )
            else:
                err_text = mask_secret(resp.text, access_token)
                raise RuntimeError(f"Threads API server error ({resp.status_code}): {err_text}")

        except httpx.RequestError as exc:
            raise RuntimeError(f"Network error querying Threads media insights: {mask_secret(str(exc), access_token)}")
        finally:
            if self.client is None and hasattr(client_ctx, "close"):
                client_ctx.close()

    def get_account_insights(self, db: Session) -> Dict[str, Any]:
        """
        Fetches account-level aggregate and daily metrics.
        Queries GET /v1.0/{threads-user-id}/threads_insights?metric=views,likes,replies,reposts,quotes,clicks,followers_count.
        Handles missing metrics or small accounts gracefully by storing null.
        """
        token_info = ensure_fresh_token(db)
        access_token = token_info["access_token"]
        user_id = token_info.get("user_id") or "me"

        url = f"{GRAPH_HOST}/v1.0/{user_id}/threads_insights"
        params = {
            "metric": "views,likes,replies,reposts,quotes,clicks,followers_count",
            "access_token": access_token,
        }

        client_ctx = self.client or httpx.Client(timeout=20.0)
        try:
            resp = client_ctx.get(url, params=params)

            if resp.status_code == 200:
                data = resp.json()
                metrics_data = data.get("data", [])

                parsed: Dict[str, Optional[int]] = {
                    "followers_count": None,
                    "views": None,
                    "likes": None,
                    "replies": None,
                    "reposts": None,
                    "quotes": None,
                    "clicks": None,
                }

                for item in metrics_data:
                    name = item.get("name")
                    if name in parsed:
                        val = None
                        if "total_value" in item and isinstance(item["total_value"], dict):
                            val = item["total_value"].get("value")
                        elif "values" in item and isinstance(item["values"], list) and len(item["values"]) > 0:
                            # Sum daily counts if array or take latest
                            val = item["values"][-1].get("value")
                        try:
                            parsed[name] = int(val) if val is not None else None
                        except (ValueError, TypeError):
                            parsed[name] = None

                # Clean raw JSON to remove paging URL tokens
                clean_payload = {k: v for k, v in data.items() if k != "paging"}
                raw_json_str = mask_secret(json.dumps(clean_payload), access_token)

                return {
                    **parsed,
                    "raw_json": raw_json_str,
                }

            elif resp.status_code in (400, 401, 403):
                raise ValueError(
                    "Missing threads_manage_insights permission or token expired. "
                    "Generate a new token and run /dev/threads-reload-token."
                )
            else:
                err_text = mask_secret(resp.text, access_token)
                raise RuntimeError(f"Threads API server error ({resp.status_code}): {err_text}")

        except httpx.RequestError as exc:
            raise RuntimeError(f"Network error querying Threads account insights: {mask_secret(str(exc), access_token)}")
        finally:
            if self.client is None and hasattr(client_ctx, "close"):
                client_ctx.close()

    def delete(self, target: Any) -> DeleteResult:
        """
        Deletes published post(s) from Threads.
        For multi-part chains, deletes all part IDs stored in publish_state.
        Queries DELETE /v1.0/{media-id}?access_token={token}.
        """
        existing_session = object_session(target) if target is not None else None
        if existing_session is not None:
            return self._execute_delete(target, existing_session)

        with SessionLocal() as db:
            return self._execute_delete(target, db)

    def _execute_delete(self, target: Any, db: Session) -> DeleteResult:
        try:
            token_info = ensure_fresh_token(db)
        except Exception as exc:
            return DeleteResult(success=False, error_message=f"Authentication error: {exc}")

        access_token = token_info["access_token"]

        # Collect part IDs
        part_ids: List[str] = []
        if getattr(target, "publish_state", None):
            try:
                state = json.loads(target.publish_state)
                part_ids = state.get("published_part_ids", [])
            except Exception:
                pass

        if not part_ids and getattr(target, "external_post_id", None):
            part_ids = [str(target.external_post_id).strip()]

        if not part_ids:
            return DeleteResult(success=False, error_message="No Threads post ID found for target")

        client_ctx = self.client or httpx.Client(timeout=20.0)
        try:
            for pid in part_ids:
                url = f"{GRAPH_HOST}/v1.0/{pid}"
                resp = client_ctx.delete(url, params={"access_token": access_token})

                if resp.status_code in (200, 204):
                    continue

                # Handle permission or missing errors
                if resp.status_code in (400, 403, 404):
                    err_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                    err_msg = err_data.get("error", {}).get("message", f"HTTP {resp.status_code}")
                    return DeleteResult(
                        success=False,
                        error_message=f"Threads delete error: {mask_secret(err_msg, access_token)} (Ensure threads_delete permission is granted).",
                    )
                else:
                    return DeleteResult(
                        success=False,
                        error_message=f"Threads server error ({resp.status_code}): {mask_secret(resp.text, access_token)}",
                    )

            return DeleteResult(success=True)

        except Exception as exc:
            return DeleteResult(success=False, error_message=mask_secret(str(exc), access_token))
        finally:
            if self.client is None and hasattr(client_ctx, "close"):
                client_ctx.close()


