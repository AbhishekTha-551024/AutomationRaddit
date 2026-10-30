"""
Web view router providing HTML screens for the social media scheduler.
Renders Dashboard, Composer, Posts List, Post Edit, Trash, Calendar,
Post Detail, Analytics, and Playbook using Jinja2 templates.
"""

import calendar
import json
from datetime import datetime, timezone, date, timedelta
from typing import Optional, List, Dict, Any
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, Request, Query, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, and_, or_
from sqlalchemy.orm import Session
from app.database import get_db
from app.config import settings
from app.models import Post, PostTarget, Log, TargetStatus
from app.adapters.registry import SUPPORTED_PLATFORMS
from app.api.posts import format_to_ist
from app.services.queue_service import is_publishing_paused, is_overdue
from app.services.post_operations import friendly_error_message

router = APIRouter(tags=["Web UI"])

templates = Jinja2Templates(directory="app/templates")
IST = ZoneInfo("Asia/Kolkata")


def format_relative_time(target_utc: datetime) -> str:
    """Returns human-friendly relative time such as 'in 2 hours' or 'due now'."""
    now_utc = datetime.now(timezone.utc)
    if target_utc.tzinfo is None:
        target_utc = target_utc.replace(tzinfo=timezone.utc)

    diff = target_utc - now_utc
    total_seconds = int(diff.total_seconds())

    if total_seconds <= 0:
        return "due now"

    minutes = total_seconds // 60
    hours = minutes // 60
    days = hours // 24

    if days > 0:
        return f"in {days}d"
    if hours > 0:
        return f"in {hours}h"
    if minutes > 0:
        return f"in {minutes}m"
    return "in seconds"


@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_view(request: Request, db: Session = Depends(get_db)):
    """
    Renders the main dashboard screen:
    - Global paused warning (if active)
    - Overdue approval banner (>6h overdue posts)
    - Failed posts banner with friendly error messages and action triggers
    - Quiet summary stats
    - Up next 5 scheduled items
    - Recent activity table
    """
    now_utc = datetime.now(timezone.utc)
    is_paused = is_publishing_paused(db)

    # 1. Stats count (excludes soft-deleted items)
    stats_query = (
        select(PostTarget.status, func.count(PostTarget.id))
        .where(PostTarget.deleted_at.is_(None))
        .group_by(PostTarget.status)
    )
    rows = db.execute(stats_query).all()
    stats = {s.value: 0 for s in TargetStatus}
    total = 0
    for st, count in rows:
        key = st.value if hasattr(st, "value") else str(st)
        stats[key] = count
        total += count
    stats["total"] = total

    # 2. Failed targets (only active non-deleted)
    failed_stmt = (
        select(PostTarget)
        .where(
            PostTarget.status == TargetStatus.failed,
            PostTarget.deleted_at.is_(None),
        )
        .order_by(PostTarget.id.desc())
    )
    failed_rows = db.scalars(failed_stmt).all()
    failed_targets = [
        {
            "id": t.id,
            "post_id": t.post_id,
            "platform": t.platform,
            "post_title": t.post.title if t.post else None,
            "post_body": t.post.body if t.post else "",
            "error_message": t.error_message,
            "friendly_error": friendly_error_message(t.error_message),
            "attempts": t.attempts,
        }
        for t in failed_rows
    ]

    # 3. Overdue pending targets (> OVERDUE_GRACE_HOURS hours overdue)
    overdue_cutoff = now_utc - timedelta(hours=settings.OVERDUE_GRACE_HOURS)
    overdue_stmt = (
        select(PostTarget)
        .where(
            PostTarget.status == TargetStatus.pending,
            PostTarget.scheduled_at <= overdue_cutoff,
            PostTarget.deleted_at.is_(None),
        )
        .order_by(PostTarget.scheduled_at.asc())
    )
    overdue_rows = db.scalars(overdue_stmt).all()
    overdue_targets = [
        {
            "id": t.id,
            "post_id": t.post_id,
            "platform": t.platform,
            "post_title": t.post.title if t.post else None,
            "post_body": t.post.body if t.post else "",
            "scheduled_at_ist": format_to_ist(t.scheduled_at),
            "hours_overdue": round((now_utc - (t.scheduled_at if t.scheduled_at.tzinfo else t.scheduled_at.replace(tzinfo=timezone.utc))).total_seconds() / 3600, 1),
        }
        for t in overdue_rows
    ]

    # 4. Up next: next 5 scheduled pending targets
    up_next_stmt = (
        select(PostTarget)
        .where(
            PostTarget.status == TargetStatus.pending,
            PostTarget.deleted_at.is_(None),
        )
        .order_by(PostTarget.scheduled_at.asc())
        .limit(5)
    )
    up_next_rows = db.scalars(up_next_stmt).all()
    up_next = [
        {
            "post_id": t.post_id,
            "target_id": t.id,
            "platform": t.platform,
            "post_title": t.post.title if t.post else None,
            "post_body": t.post.body if t.post else "",
            "scheduled_at_ist": format_to_ist(t.scheduled_at),
            "relative_time": format_relative_time(t.scheduled_at),
            "is_overdue": is_overdue(t.scheduled_at),
        }
        for t in up_next_rows
    ]

    # 5. Recent activity: last 15 updated/created targets
    recent_stmt = (
        select(PostTarget)
        .where(PostTarget.deleted_at.is_(None))
        .order_by(PostTarget.id.desc())
        .limit(15)
    )
    recent_rows = db.scalars(recent_stmt).all()
    recent_activity = [
        {
            "post_id": t.post_id,
            "target_id": t.id,
            "platform": t.platform,
            "title": t.post.title if t.post else None,
            "body": t.post.body if t.post else "",
            "status": t.status.value,
            "time_ist": format_to_ist(t.scheduled_at),
            "external_id": t.external_post_id,
            "error_message": t.error_message,
        }
        for t in recent_rows
    ]

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "active_page": "dashboard",
            "is_paused": is_paused,
            "stats": stats,
            "failed_targets": failed_targets,
            "overdue_targets": overdue_targets,
            "up_next": up_next,
            "recent_activity": recent_activity,
        },
    )


@router.get("/posts", response_class=HTMLResponse)
def posts_list_view(
    request: Request,
    status_filter: Optional[str] = Query("all", alias="status"),
    platform_filter: Optional[str] = Query("all", alias="platform"),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    sort_by: Optional[str] = Query("scheduled_desc", alias="sort"),
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """
    Renders the Posts management table:
    - Filters: status, platform, date range (from/to), text search
    - Sort: scheduled or created time (asc/desc)
    - Pagination: 25 items per page
    - Table columns: Title/Content, Platforms (status icon per platform), Scheduled time (IST), Overall status, Action menu (...)
    """
    stmt = (
        select(Post)
        .where(Post.deleted_at.is_(None))
    )

    # Status filter
    if status_filter and status_filter != "all":
        try:
            target_status = TargetStatus(status_filter)
            stmt = stmt.where(
                Post.targets.any(
                    and_(
                        PostTarget.status == target_status,
                        PostTarget.deleted_at.is_(None),
                    )
                )
            )
        except ValueError:
            pass

    # Platform filter
    if platform_filter and platform_filter != "all":
        stmt = stmt.where(
            Post.targets.any(
                and_(
                    PostTarget.platform == platform_filter.lower(),
                    PostTarget.deleted_at.is_(None),
                )
            )
        )

    # Date range filter (scheduled date in IST)
    if date_from and date_from.strip():
        try:
            df = date.fromisoformat(date_from.strip())
            start_utc = datetime.combine(df, datetime.min.time(), tzinfo=IST).astimezone(timezone.utc)
            stmt = stmt.where(
                Post.targets.any(
                    and_(
                        PostTarget.scheduled_at >= start_utc,
                        PostTarget.deleted_at.is_(None),
                    )
                )
            )
        except Exception:
            pass

    if date_to and date_to.strip():
        try:
            dt = date.fromisoformat(date_to.strip())
            end_utc = datetime.combine(dt, datetime.max.time(), tzinfo=IST).astimezone(timezone.utc)
            stmt = stmt.where(
                Post.targets.any(
                    and_(
                        PostTarget.scheduled_at <= end_utc,
                        PostTarget.deleted_at.is_(None),
                    )
                )
            )
        except Exception:
            pass

    # Text search in title or body
    if search and search.strip():
        term = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                Post.title.ilike(term),
                Post.body.ilike(term),
            )
        )

    # Sorting
    if sort_by == "created_asc":
        stmt = stmt.order_by(Post.created_at.asc())
    elif sort_by == "created_desc":
        stmt = stmt.order_by(Post.created_at.desc())
    else:  # default scheduled_desc or scheduled_asc
        # Order by post id/created as proxy or targets
        if sort_by == "scheduled_asc":
            stmt = stmt.order_by(Post.created_at.asc())
        else:
            stmt = stmt.order_by(Post.created_at.desc())

    # Total count for pagination
    subq = stmt.subquery()
    count_stmt = select(func.count()).select_from(subq)
    total_count = db.scalar(count_stmt) or 0
    total_pages = max(1, (total_count + limit - 1) // limit)

    offset = (page - 1) * limit
    paged_stmt = stmt.offset(offset).limit(limit)
    posts = db.scalars(paged_stmt).all()

    items = []
    for p in posts:
        active_targets = [t for t in p.targets if t.deleted_at is None]
        if not active_targets:
            continue

        # Determine overall status
        statuses = [t.status for t in active_targets]
        if any(s == TargetStatus.processing for s in statuses):
            overall_status = "processing"
        elif all(s == TargetStatus.posted for s in statuses):
            overall_status = "posted"
        elif all(s == TargetStatus.draft for s in statuses):
            overall_status = "draft"
        elif any(s == TargetStatus.failed for s in statuses):
            overall_status = "failed"
        elif all(s == TargetStatus.cancelled for s in statuses):
            overall_status = "cancelled"
        else:
            overall_status = "pending"

        # Scheduled time: choose earliest pending target or first target
        pending_targets = [t for t in active_targets if t.status == TargetStatus.pending]
        chosen_target = pending_targets[0] if pending_targets else active_targets[0]

        # Platforms summary with status icon
        plat_list = [
            {
                "id": t.id,
                "platform": t.platform,
                "status": t.status.value,
                "scheduled_at_ist": format_to_ist(t.scheduled_at),
                "error_message": t.error_message,
            }
            for t in active_targets
        ]

        # Friendly error from any failed target
        friendly_err = None
        for t in active_targets:
            if t.status == TargetStatus.failed and t.error_message:
                friendly_err = friendly_error_message(t.error_message)
                break

        items.append({
            "id": p.id,
            "title": p.title,
            "body": p.body,
            "media_url": p.media_url,
            "created_at_ist": format_to_ist(p.created_at),
            "edited_at_ist": format_to_ist(p.edited_at),
            "scheduled_at_ist": format_to_ist(chosen_target.scheduled_at),
            "relative_time": format_relative_time(chosen_target.scheduled_at),
            "overall_status": overall_status,
            "primary_target_id": chosen_target.id,
            "platforms": plat_list,
            "friendly_error": friendly_err,
        })

    return templates.TemplateResponse(
        request=request,
        name="posts_list.html",
        context={
            "active_page": "posts",
            "items": items,
            "total_count": total_count,
            "current_page": page,
            "total_pages": total_pages,
            "status_filter": status_filter,
            "platform_filter": platform_filter,
            "date_from": date_from or "",
            "date_to": date_to or "",
            "search": search or "",
            "sort_by": sort_by,
            "supported_platforms": sorted(list(SUPPORTED_PLATFORMS)),
        },
    )


@router.get("/posts/new", response_class=HTMLResponse)
def composer_view(request: Request, db: Session = Depends(get_db)):
    """
    Renders the Composer (New post) creation screen with live preview and Save as draft.
    """
    is_paused = is_publishing_paused(db)
    return templates.TemplateResponse(
        request=request,
        name="composer.html",
        context={
            "active_page": "composer",
            "is_paused": is_paused,
            "supported_platforms": sorted(list(SUPPORTED_PLATFORMS)),
        },
    )


@router.get("/posts/{post_id}/edit", response_class=HTMLResponse)
def post_edit_view(post_id: int, request: Request, db: Session = Depends(get_db)):
    """
    Renders the post edit screen.
    Enforces status rules: locked if any target is processing or posted.
    """
    post = db.get(Post, post_id)
    if not post or post.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found or in Trash.")

    is_paused = is_publishing_paused(db)

    is_locked = False
    locked_reason = ""
    for t in post.targets:
        if t.status == TargetStatus.processing:
            is_locked = True
            locked_reason = f"Target #{t.id} ({t.platform}) is currently sending. Editing is blocked until transmission completes."
            break
        elif t.status == TargetStatus.posted:
            is_locked = True
            locked_reason = (
                "Editing is not allowed because this post has already been published to one or more platforms. "
                "Threads and social platforms do not support editing published posts. Use 'Duplicate' to create an editable new copy."
            )
            break

    target_items = [
        {
            "id": t.id,
            "platform": t.platform,
            "status": t.status.value,
            "scheduled_at_ist": format_to_ist(t.scheduled_at),
        }
        for t in post.targets
        if t.deleted_at is None
    ]
    active_platforms = [t.platform for t in post.targets if t.deleted_at is None]

    first_target = next((t for t in post.targets if t.deleted_at is None), None)
    sched_local_str = ""
    if first_target and first_target.scheduled_at:
        dt_utc = first_target.scheduled_at if first_target.scheduled_at.tzinfo else first_target.scheduled_at.replace(tzinfo=timezone.utc)
        dt_ist = dt_utc.astimezone(IST)
        sched_local_str = dt_ist.strftime("%Y-%m-%dT%H:%M")

    return templates.TemplateResponse(
        request=request,
        name="edit_post.html",
        context={
            "active_page": "posts",
            "is_paused": is_paused,
            "post": {
                "id": post.id,
                "title": post.title or "",
                "body": post.body,
                "media_url": post.media_url or "",
                "targets": target_items,
                "active_platforms": active_platforms,
                "scheduled_datetime": sched_local_str,
            },
            "is_locked": is_locked,
            "locked_reason": locked_reason,
            "supported_platforms": sorted(list(SUPPORTED_PLATFORMS)),
        },
    )


@router.get("/trash", response_class=HTMLResponse)
def trash_view(
    request: Request,
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """
    Renders the Trash management screen.
    Lists soft-deleted posts and targets, allows restore and permanent deletion,
    and displays the 30-day auto-purge advisory.
    """
    is_paused = is_publishing_paused(db)

    # Query soft-deleted targets
    stmt = (
        select(PostTarget)
        .where(PostTarget.deleted_at.is_not(None))
        .order_by(PostTarget.deleted_at.desc())
    )
    count_stmt = select(func.count(PostTarget.id)).where(PostTarget.deleted_at.is_not(None))
    total_count = db.scalar(count_stmt) or 0
    total_pages = max(1, (total_count + limit - 1) // limit)

    offset = (page - 1) * limit
    trash_rows = db.scalars(stmt.offset(offset).limit(limit)).all()

    items = [
        {
            "target_id": t.id,
            "post_id": t.post_id,
            "platform": t.platform,
            "title": t.post.title if t.post else None,
            "body": t.post.body if t.post else "",
            "status": t.status.value,
            "deleted_at_ist": format_to_ist(t.deleted_at),
        }
        for t in trash_rows
    ]

    return templates.TemplateResponse(
        request=request,
        name="trash.html",
        context={
            "active_page": "trash",
            "is_paused": is_paused,
            "items": items,
            "total_count": total_count,
            "current_page": page,
            "total_pages": total_pages,
        },
    )


@router.get("/calendar", response_class=HTMLResponse)
def calendar_view(
    request: Request,
    year: Optional[int] = Query(None),
    month: Optional[int] = Query(None),
    selected_day: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Renders the calendar month view with scheduled posts placed on their respective days.
    Excludes soft-deleted items.
    """
    is_paused = is_publishing_paused(db)
    now_ist = datetime.now(IST)
    target_year = year or now_ist.year
    target_month = month or now_ist.month

    # Prev and Next month navigation
    first_of_month = date(target_year, target_month, 1)
    prev_month_date = (first_of_month - timedelta(days=1)).replace(day=1)
    next_month_date = (first_of_month + timedelta(days=32)).replace(day=1)

    cal = calendar.Calendar(firstweekday=0)  # 0 = Monday
    weeks = cal.monthdatescalendar(target_year, target_month)

    # Query all active targets in the month window
    start_bound = datetime.combine(weeks[0][0], datetime.min.time(), tzinfo=IST).astimezone(timezone.utc)
    end_bound = datetime.combine(weeks[-1][-1], datetime.max.time(), tzinfo=IST).astimezone(timezone.utc)

    stmt = (
        select(PostTarget)
        .where(
            PostTarget.deleted_at.is_(None),
            PostTarget.scheduled_at >= start_bound,
            PostTarget.scheduled_at <= end_bound,
        )
    )
    targets = db.scalars(stmt).all()

    # Map posts by day string 'YYYY-MM-DD'
    posts_by_day: Dict[str, List[Dict[str, Any]]] = {}
    for t in targets:
        dt_utc = t.scheduled_at if t.scheduled_at.tzinfo else t.scheduled_at.replace(tzinfo=timezone.utc)
        dt_ist = dt_utc.astimezone(IST)
        day_key = dt_ist.strftime("%Y-%m-%d")

        if day_key not in posts_by_day:
            posts_by_day[day_key] = []

        posts_by_day[day_key].append({
            "post_id": t.post_id,
            "target_id": t.id,
            "platform": t.platform,
            "status": t.status.value,
            "title": t.post.title if t.post else None,
            "body": t.post.body if t.post else "",
            "time_str": dt_ist.strftime("%H:%M"),
            "time_ist": dt_ist.strftime("%I:%M %p IST"),
        })

    today_str = now_ist.strftime("%Y-%m-%d")
    chosen_day_str = selected_day or today_str

    calendar_cells = []
    for week in weeks:
        for d in week:
            d_str = d.strftime("%Y-%m-%d")
            calendar_cells.append({
                "date_str": d_str,
                "day_number": d.day,
                "in_month": (d.month == target_month),
                "is_today": (d_str == today_str),
                "posts": posts_by_day.get(d_str, []),
            })

    selected_day_posts = posts_by_day.get(chosen_day_str, [])

    try:
        sel_date_obj = date.fromisoformat(chosen_day_str)
        selected_date_label = sel_date_obj.strftime("%A, %d %B %Y")
    except Exception:
        selected_date_label = chosen_day_str

    return templates.TemplateResponse(
        request=request,
        name="calendar.html",
        context={
            "active_page": "calendar",
            "is_paused": is_paused,
            "current_year": target_year,
            "current_month": target_month,
            "current_month_name": first_of_month.strftime("%B"),
            "prev_year": prev_month_date.year,
            "prev_month": prev_month_date.month,
            "next_year": next_month_date.year,
            "next_month": next_month_date.month,
            "calendar_cells": calendar_cells,
            "selected_day_posts": selected_day_posts,
            "selected_date_label": selected_date_label,
        },
    )


@router.get("/posts/{post_id}/view", response_class=HTMLResponse)
def post_detail_view(post_id: int, request: Request, db: Session = Depends(get_db)):
    """
    Renders the post detail screen showing the post body, target status badges,
    action controls (Post now, Cancel, Reschedule, Delete, Duplicate), and audit log timeline.
    """
    post = db.get(Post, post_id)
    if not post:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Post #{post_id} not found",
        )

    is_paused = is_publishing_paused(db)

    targets_data = []
    target_ids = []
    for t in post.targets:
        target_ids.append(t.id)
        targets_data.append({
            "id": t.id,
            "platform": t.platform,
            "status": t.status.value,
            "scheduled_at_ist": format_to_ist(t.scheduled_at),
            "next_retry_at_ist": format_to_ist(t.next_retry_at),
            "attempts": t.attempts,
            "external_post_id": t.external_post_id,
            "error_message": t.error_message,
            "friendly_error": friendly_error_message(t.error_message) if t.status == TargetStatus.failed else None,
            "publish_state": t.publish_state,
            "deleted_at": t.deleted_at,
        })

    logs_stmt = (
        select(Log)
        .where(Log.post_target_id.in_(target_ids))
        .order_by(Log.created_at.asc())
    )
    logs_rows = db.scalars(logs_stmt).all() if target_ids else []

    formatted_logs = [
        {
            "id": l.id,
            "post_target_id": l.post_target_id,
            "level": l.level,
            "message": l.message,
            "created_at_ist": format_to_ist(l.created_at),
        }
        for l in logs_rows
    ]

    post_data = {
        "id": post.id,
        "title": post.title,
        "body": post.body,
        "media_url": post.media_url,
        "created_at_ist": format_to_ist(post.created_at),
        "edited_at_ist": format_to_ist(post.edited_at),
        "deleted_at": post.deleted_at,
        "targets": targets_data,
    }

    return templates.TemplateResponse(
        request=request,
        name="post_detail.html",
        context={
            "active_page": "posts",
            "is_paused": is_paused,
            "post": post_data,
            "logs": formatted_logs,
        },
    )


@router.get("/analytics", response_class=HTMLResponse)
def analytics_view(request: Request, db: Session = Depends(get_db)):
    """
    Renders the Analytics dashboard.
    Note: includes data from trashed posts.
    """
    from app.services.analytics import get_analytics_summary, get_scored_posts
    from app.services.patterns import analyze_patterns
    from app.services.analyst import check_analyst_readiness
    from app.models.analyst import AnalysisReport
    from app.models.metric import AccountSnapshot

    is_paused = is_publishing_paused(db)
    summary = get_analytics_summary(db, days=30)
    posts = get_scored_posts(db, days=30)
    patterns_data = analyze_patterns(db, days=90)
    analyst_status = check_analyst_readiness(db)

    latest_report = db.scalars(
        select(AnalysisReport).order_by(AnalysisReport.created_at.desc())
    ).first()

    report_parsed = {}
    if latest_report and latest_report.findings_json:
        try:
            report_parsed = json.loads(latest_report.findings_json)
        except Exception:
            pass

    snap_stmt = select(AccountSnapshot).order_by(AccountSnapshot.snapshot_date.asc())
    follower_snaps = list(db.scalars(snap_stmt).all())

    return templates.TemplateResponse(
        request=request,
        name="analytics.html",
        context={
            "active_page": "analytics",
            "is_paused": is_paused,
            "summary": summary,
            "posts": posts,
            "patterns": patterns_data,
            "analyst_status": analyst_status,
            "latest_report": latest_report,
            "report_findings": report_parsed,
            "follower_snaps": follower_snaps,
        },
    )


@router.get("/playbook", response_class=HTMLResponse)
def playbook_view(request: Request, db: Session = Depends(get_db)):
    """
    Renders the Playbook screen.
    """
    from app.services.playbook_service import list_rules, get_active_playbook_text
    from app.services.analyst import check_analyst_readiness

    is_paused = is_publishing_paused(db)
    active_rules = list_rules(db, status="active")
    suggested_rules = list_rules(db, status="suggested")
    rejected_rules = list_rules(db, status="rejected")
    export_text = get_active_playbook_text(db)
    analyst_status = check_analyst_readiness(db)

    return templates.TemplateResponse(
        request=request,
        name="playbook.html",
        context={
            "active_page": "playbook",
            "is_paused": is_paused,
            "active_rules": active_rules,
            "suggested_rules": suggested_rules,
            "rejected_rules": rejected_rules,
            "export_text": export_text,
            "analyst_status": analyst_status,
        },
    )


@router.get("/connections", response_class=HTMLResponse)
@router.get("/settings", response_class=HTMLResponse)
def connections_view(request: Request, db: Session = Depends(get_db)):
    """
    Renders the Platform Connections & Operational Settings screen.
    Displays health status, account identity, token expiry, failure counts,
    and Telegram alert settings.
    """
    from app.services.connection_service import get_all_connections_summary, mask_credential_value
    from app.services.alerts import is_alerts_enabled
    from app.models.alert import AlertLog

    is_paused = is_publishing_paused(db)
    connections_data = get_all_connections_summary(db)

    # Format timestamps to IST for template presentation
    for conn in connections_data:
        if conn.get("token_expiry"):
            conn["token_expiry_ist"] = format_to_ist(conn["token_expiry"])
        if conn.get("last_test_at"):
            conn["last_test_at_ist"] = format_to_ist(conn["last_test_at"])
        if conn.get("last_success_at"):
            conn["last_success_at_ist"] = format_to_ist(conn["last_success_at"])

    # Query last 20 alert logs
    stmt_alerts = select(AlertLog).order_by(AlertLog.id.desc()).limit(20)
    alert_logs = db.scalars(stmt_alerts).all()
    formatted_alert_logs = []
    for a in alert_logs:
        formatted_alert_logs.append({
            "id": a.id,
            "kind": a.kind,
            "sent_at_ist": format_to_ist(a.sent_at),
            "success": a.success,
            "error": a.error,
            "preview": a.message_preview,
        })

    alerts_enabled = is_alerts_enabled(db)
    alert_chat_masked = mask_credential_value(settings.TELEGRAM_ALERT_CHAT_ID)

    quiet_hours = "None"
    if settings.ALERT_QUIET_HOURS_START and settings.ALERT_QUIET_HOURS_END:
        quiet_hours = f"{settings.ALERT_QUIET_HOURS_START} – {settings.ALERT_QUIET_HOURS_END} IST"

    return templates.TemplateResponse(
        request=request,
        name="connections.html",
        context={
            "active_page": "connections",
            "is_paused": is_paused,
            "connections": connections_data,
            "alerts_enabled": alerts_enabled,
            "alert_chat_masked": alert_chat_masked,
            "quiet_hours": quiet_hours,
            "alert_digest_hour": f"{settings.ALERT_DIGEST_HOUR_IST}:00 IST",
            "alert_logs": formatted_alert_logs,
        },
    )

