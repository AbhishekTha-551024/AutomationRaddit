"""
Posts API router.
Handles post creation (including drafts) with target scheduling,
editing unpublished posts with platform validation,
soft deletion, restoration, duplication, and listing with IST timestamps.
"""

from datetime import datetime, timezone
from typing import List, Optional, Union
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Post, PostTarget, TargetStatus, Log
from app.adapters.registry import is_platform_supported, SUPPORTED_PLATFORMS
from app.services.post_operations import (
    soft_delete_post,
    restore_post,
    edit_post_content,
    duplicate_post,
    validate_content_for_platforms,
)

router = APIRouter(prefix="/posts", tags=["Posts & Scheduling"])

# Indian Standard Time (IST, UTC+5:30)
IST = ZoneInfo("Asia/Kolkata")


def parse_to_utc(dt_val: Union[datetime, str]) -> datetime:
    """
    Parses an ISO datetime string or datetime object and converts it to UTC.
    If the input has no timezone attached (naive), it assumes Asia/Kolkata (IST).
    """
    if isinstance(dt_val, str):
        clean_str = dt_val.replace("Z", "+00:00")
        parsed_dt = datetime.fromisoformat(clean_str)
    else:
        parsed_dt = dt_val

    if parsed_dt.tzinfo is None:
        parsed_dt = parsed_dt.replace(tzinfo=IST)

    return parsed_dt.astimezone(timezone.utc)


def format_to_ist(dt: Optional[datetime]) -> Optional[str]:
    """
    Converts a UTC datetime to a human-readable IST string for display.
    """
    if dt is None:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S IST")


# --- Pydantic Request & Response Schemas ---

class TargetInput(BaseModel):
    """Input payload for a single target platform schedule."""
    platform: str = Field(..., description="Target platform: 'telegram', 'discord', 'threads'")
    scheduled_at: Union[datetime, str] = Field(
        ...,
        description="Scheduled ISO datetime (e.g. '2026-10-30T10:00:00'). Naive times assume IST.",
    )

    @field_validator("platform")
    @classmethod
    def validate_platform(cls, v: str) -> str:
        clean = v.strip().lower()
        if not is_platform_supported(clean):
            raise ValueError(
                f"Unsupported platform '{v}'. Supported platforms are: {sorted(list(SUPPORTED_PLATFORMS))}."
            )
        return clean


class PostCreateRequest(BaseModel):
    """Payload to create a post and schedule its platforms."""
    title: Optional[str] = Field(None, max_length=255, description="Optional post title")
    body: str = Field(..., min_length=1, description="Message text or caption")
    media_url: Optional[str] = Field(None, max_length=1000, description="Public image or video URL (required for Instagram)")
    targets: List[TargetInput] = Field(..., min_items=1, description="One or more target platforms")
    is_draft: bool = Field(False, description="Whether to save targets as drafts without queueing for publish")


class PostEditRequest(BaseModel):
    """Payload to edit title, body, and media of an existing post."""
    title: Optional[str] = Field(None, max_length=255)
    body: str = Field(..., min_length=1)
    media_url: Optional[str] = Field(None, max_length=1000)
    platforms: Optional[List[str]] = Field(None, description="Optional target platforms to add/remove on unpublished post")


class TargetResponse(BaseModel):
    """Target platform status response with time in IST."""
    id: int
    platform: str
    status: str
    scheduled_at_ist: str
    next_retry_at_ist: Optional[str] = None
    attempts: int
    external_post_id: Optional[str] = None
    error_message: Optional[str] = None
    publish_state: Optional[str] = None
    deleted_at_ist: Optional[str] = None


class PostResponse(BaseModel):
    """Post details response with associated targets."""
    id: int
    title: Optional[str] = None
    body: str
    media_url: Optional[str] = None
    created_at_ist: str
    edited_at_ist: Optional[str] = None
    deleted_at_ist: Optional[str] = None
    targets: List[TargetResponse]


def build_post_response(post: Post) -> PostResponse:
    return PostResponse(
        id=post.id,
        title=post.title,
        body=post.body,
        media_url=post.media_url,
        created_at_ist=format_to_ist(post.created_at) or "",
        edited_at_ist=format_to_ist(post.edited_at),
        deleted_at_ist=format_to_ist(post.deleted_at),
        targets=[
            TargetResponse(
                id=t.id,
                platform=t.platform,
                status=t.status.value,
                scheduled_at_ist=format_to_ist(t.scheduled_at) or "",
                next_retry_at_ist=format_to_ist(t.next_retry_at),
                attempts=t.attempts,
                external_post_id=t.external_post_id,
                error_message=t.error_message,
                publish_state=t.publish_state,
                deleted_at_ist=format_to_ist(t.deleted_at),
            )
            for t in post.targets
        ],
    )


# --- Route Handlers ---

@router.post("", response_model=PostResponse, status_code=status.HTTP_201_CREATED)
def create_post(payload: PostCreateRequest, db: Session = Depends(get_db)):
    """
    Create a new post and schedule it across one or more platforms.
    If is_draft is True, creates targets in 'draft' status.
    All scheduled timestamps are converted to UTC before storage.
    """
    # 1. Validate all platforms before creating the post
    target_platforms = [item.platform.strip().lower() for item in payload.targets]
    for p_name in target_platforms:
        if not is_platform_supported(p_name):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unsupported platform '{p_name}'. Supported platforms: {sorted(list(SUPPORTED_PLATFORMS))}.",
            )

    # 2. Validate content limits for chosen platforms
    try:
        validate_content_for_platforms(payload.title, payload.body, payload.media_url, target_platforms)
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(ve))

    # 3. Create the parent post
    post = Post(
        title=payload.title,
        body=payload.body,
        media_url=payload.media_url,
    )
    db.add(post)
    db.flush()

    target_status = TargetStatus.draft if payload.is_draft else TargetStatus.pending

    # 4. Create targets
    for item in payload.targets:
        utc_schedule_time = parse_to_utc(item.scheduled_at)

        target = PostTarget(
            post_id=post.id,
            platform=item.platform.strip().lower(),
            scheduled_at=utc_schedule_time,
            status=target_status,
            attempts=0,
        )
        db.add(target)

    db.commit()
    db.refresh(post)

    return build_post_response(post)


@router.get("", response_model=List[PostResponse])
def list_posts(include_trash: bool = Query(False), db: Session = Depends(get_db)):
    """
    List all created posts and their target scheduling statuses.
    Excludes soft-deleted posts by default.
    Timestamps are converted and displayed in IST.
    """
    stmt = select(Post)
    if not include_trash:
        stmt = stmt.where(Post.deleted_at.is_(None))
    stmt = stmt.order_by(Post.created_at.desc())
    posts = db.scalars(stmt).all()

    return [build_post_response(p) for p in posts]


@router.get("/{post_id}", response_model=PostResponse)
def get_post(post_id: int, db: Session = Depends(get_db)):
    """
    Retrieve details for a single post by ID along with its targets and statuses.
    """
    post = db.get(Post, post_id)
    if not post:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Post #{post_id} not found",
        )

    return build_post_response(post)


@router.patch("/{post_id}", response_model=PostResponse)
def update_post_content(post_id: int, payload: PostEditRequest, db: Session = Depends(get_db)):
    """
    Edit title, body, and media of an existing post.
    Strictly blocked with 409 if any target is currently processing or already posted.
    """
    try:
        edit_post_content(
            db=db,
            post_id=post_id,
            title=payload.title,
            body=payload.body,
            media_url=payload.media_url,
            platforms=payload.platforms,
        )
    except ValueError as ve:
        err_msg = str(ve)
        if "not found" in err_msg.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=err_msg)
        elif "exceeds" in err_msg.lower() or "requires" in err_msg.lower():
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=err_msg)
        else:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=err_msg)

    post = db.get(Post, post_id)
    return build_post_response(post)


@router.delete("/{post_id}")
def delete_post(
    post_id: int,
    also_delete_platform: bool = Query(False),
    db: Session = Depends(get_db),
):
    """
    Soft-deletes a post and all its targets.
    Optionally deletes posted items from platforms if also_delete_platform is true.
    Blocked with 409 if any target is currently sending.
    """
    try:
        res = soft_delete_post(db, post_id=post_id, also_delete_platform=also_delete_platform)
        return res
    except ValueError as ve:
        err_msg = str(ve)
        if "not found" in err_msg.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=err_msg)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=err_msg)


@router.post("/{post_id}/restore")
def restore_deleted_post(post_id: int, db: Session = Depends(get_db)):
    """
    Restores a soft-deleted post and all its targets from Trash.
    """
    try:
        res = restore_post(db, post_id=post_id)
        return res
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(ve))


@router.post("/{post_id}/duplicate")
def duplicate_existing_post(post_id: int, db: Session = Depends(get_db)):
    """
    Creates a new draft post with the same text, media, and target platforms.
    """
    try:
        new_post_id = duplicate_post(db, post_id=post_id)
        return {"status": "success", "new_post_id": new_post_id}
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(ve))


@router.post("/{post_id}/targets", response_model=PostResponse)
def add_target_to_post(post_id: int, payload: TargetInput, db: Session = Depends(get_db)):
    """
    Adds a new target platform to an existing post.
    Allowed only if the post is not deleted and no targets are actively sending.
    """
    post = db.get(Post, post_id)
    if not post or post.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found or in Trash.")

    for t in post.targets:
        if t.status == TargetStatus.processing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot add targets while post target #{t.id} is sending.",
            )

    platform_name = payload.platform.strip().lower()

    # Check for existing active target for this platform
    for t in post.targets:
        if t.platform == platform_name and t.deleted_at is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Platform '{platform_name}' is already attached to post #{post_id}.",
            )

    # Validate content length for new platform
    try:
        validate_content_for_platforms(post.title, post.body, post.media_url, [platform_name])
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(ve))

    utc_schedule_time = parse_to_utc(payload.scheduled_at)
    target = PostTarget(
        post_id=post.id,
        platform=platform_name,
        scheduled_at=utc_schedule_time,
        status=TargetStatus.pending,
        attempts=0,
    )
    db.add(target)
    db.add(Log(
        post_target_id=target.id,
        level="INFO",
        message=f"Target platform '{platform_name}' added to post #{post.id}.",
    ))
    db.commit()
    db.refresh(post)

    return build_post_response(post)
