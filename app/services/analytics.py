"""
Analytics service for engagement performance evaluation.
Calculates weighted engagement scores, performance ratios vs median,
timing heatmaps in IST, and aggregate executive summaries.
"""

from datetime import datetime, timezone, timedelta
import statistics
from typing import List, Dict, Any, Optional
from zoneinfo import ZoneInfo
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models.target import PostTarget, TargetStatus
from app.models.metric import MetricSnapshot, AccountSnapshot
from app.models.account import Account
from app.api.posts import format_to_ist

IST = ZoneInfo("Asia/Kolkata")

# Easily configurable weighting constants for engagement calculation
# Replies, reposts, and quotes signal deeper user investment than passive likes
ENGAGEMENT_WEIGHTS = {
    "likes": 1.0,
    "replies": 3.0,
    "reposts": 3.0,
    "quotes": 3.0,
    "shares": 2.0,
}


def compute_engagement_score(
    views: Optional[int],
    likes: Optional[int],
    replies: Optional[int],
    reposts: Optional[int],
    quotes: Optional[int],
    shares: Optional[int],
) -> float:
    """
    Computes weighted engagement score:
    Score = (likes*1 + replies*3 + reposts*3 + quotes*3 + shares*2) / max(views, 1)
    """
    v = max(views or 0, 1)
    l = likes or 0
    rep = replies or 0
    rp = reposts or 0
    q = quotes or 0
    sh = shares or 0

    numerator = (
        l * ENGAGEMENT_WEIGHTS["likes"]
        + rep * ENGAGEMENT_WEIGHTS["replies"]
        + rp * ENGAGEMENT_WEIGHTS["reposts"]
        + q * ENGAGEMENT_WEIGHTS["quotes"]
        + sh * ENGAGEMENT_WEIGHTS["shares"]
    )
    return round(numerator / v, 4)


def get_latest_snapshot_for_target(db: Session, target_id: int) -> Optional[MetricSnapshot]:
    """
    Returns the most recent metric snapshot for target root (part 0).
    """
    stmt = (
        select(MetricSnapshot)
        .where(
            MetricSnapshot.post_target_id == target_id,
            MetricSnapshot.part_index == 0,
        )
        .order_by(MetricSnapshot.captured_at.desc())
    )
    return db.scalars(stmt).first()


def get_scored_posts(db: Session, days: int = 30) -> List[Dict[str, Any]]:
    """
    Retrieves all Threads posts from the last `days` days with their latest snapshot metrics,
    calculated engagement score, and 'vs average' ratio against recent posts.
    """
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(days=days)

    stmt = (
        select(PostTarget)
        .where(
            PostTarget.platform == "threads",
            PostTarget.status == TargetStatus.posted,
            PostTarget.posted_at.is_not(None),
            PostTarget.posted_at >= cutoff,
        )
        .order_by(PostTarget.posted_at.desc())
    )
    targets = db.scalars(stmt).all()

    scored_posts: List[Dict[str, Any]] = []

    for t in targets:
        snap = get_latest_snapshot_for_target(db, t.id)
        if not snap:
            continue

        score = compute_engagement_score(
            views=snap.views,
            likes=snap.likes,
            replies=snap.replies,
            reposts=snap.reposts,
            quotes=snap.quotes,
            shares=snap.shares,
        )

        post_title = t.post.title if t.post else ""
        post_body = t.post.body if t.post else ""

        scored_posts.append({
            "target_id": t.id,
            "post_id": t.post_id,
            "title": post_title or (post_body[:50] + "..." if len(post_body) > 50 else post_body),
            "body": post_body,
            "posted_at_utc": t.posted_at,
            "posted_at_ist": format_to_ist(t.posted_at),
            "views": snap.views or 0,
            "likes": snap.likes or 0,
            "replies": snap.replies or 0,
            "reposts": snap.reposts or 0,
            "quotes": snap.quotes or 0,
            "shares": snap.shares or 0,
            "score": score,
            "age_label": snap.age_label,
            "captured_at_utc": snap.captured_at,
            "captured_at_ist": format_to_ist(snap.captured_at),
            "vs_average": None,  # Computed below once sample is gathered
        })

    # Compute median score for the last 30 posts (only if at least 5 posts exist)
    recent_scored = scored_posts[:30]
    if len(recent_scored) >= 5:
        all_scores = [p["score"] for p in recent_scored]
        median_score = statistics.median(all_scores)

        for p in scored_posts:
            if median_score > 0:
                p["vs_average"] = round(p["score"] / median_score, 2)
            else:
                p["vs_average"] = 1.0

    return scored_posts


def get_analytics_summary(db: Session, days: int = 30) -> Dict[str, Any]:
    """
    Computes high-level analytics summary:
    Total engagement, average score, top 5 & bottom 5 performers,
    follower change, and data confidence rating.
    """
    posts = get_scored_posts(db, days=days)

    total_views = sum(p["views"] for p in posts)
    total_likes = sum(p["likes"] for p in posts)
    total_replies = sum(p["replies"] for p in posts)
    total_reposts = sum(p["reposts"] for p in posts)
    total_quotes = sum(p["quotes"] for p in posts)
    total_shares = sum(p["shares"] for p in posts)

    avg_score = round(sum(p["score"] for p in posts) / len(posts), 4) if posts else 0.0

    # Sort for top and bottom performers
    sorted_by_score = sorted(posts, key=lambda x: x["score"], reverse=True)
    top_5 = sorted_by_score[:5]
    bottom_5 = sorted_by_score[-5:] if len(sorted_by_score) >= 5 else []

    # Follower growth calculation from account_snapshots
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days)).date()
    snap_stmt = (
        select(AccountSnapshot)
        .where(AccountSnapshot.snapshot_date >= cutoff_date)
        .order_by(AccountSnapshot.snapshot_date.asc())
    )
    acc_snaps = db.scalars(snap_stmt).all()

    follower_change: Optional[int] = None
    if len(acc_snaps) >= 2:
        first_count = acc_snaps[0].followers_count
        latest_count = acc_snaps[-1].followers_count
        if first_count is not None and latest_count is not None:
            follower_change = latest_count - first_count

    # Data confidence flag: patterns require >= 20 scored posts
    data_confidence = "high" if len(posts) >= 20 else "low"

    return {
        "period_days": days,
        "total_posts": len(posts),
        "total_views": total_views,
        "total_likes": total_likes,
        "total_replies": total_replies,
        "total_reposts": total_reposts,
        "total_quotes": total_quotes,
        "total_shares": total_shares,
        "average_score": avg_score,
        "top_5": top_5,
        "bottom_5": bottom_5,
        "follower_change": follower_change,
        "data_confidence": data_confidence,
    }


def get_best_times_heatmap(db: Session, days: int = 60) -> Dict[str, Any]:
    """
    Computes count and average engagement score per weekday and per hour in IST.
    Does NOT make recommendations yet; outputs pure observational numbers and sample sizes.
    """
    posts = get_scored_posts(db, days=days)

    weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    # Initialize 7x24 grid: cells[(day, hour)] = [scores]
    grid: Dict[str, Dict[int, Dict[str, Any]]] = {
        d: {h: {"count": 0, "avg_score": 0.0} for h in range(24)}
        for d in weekdays
    }

    scores_by_cell: Dict[Tuple[str, int], List[float]] = {}

    for p in posts:
        dt_utc = p.get("posted_at_utc")
        if not dt_utc:
            continue
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        ist_dt = dt_utc.astimezone(IST)

        d_name = ist_dt.strftime("%A")
        h = ist_dt.hour

        key = (d_name, h)
        if key not in scores_by_cell:
            scores_by_cell[key] = []
        scores_by_cell[key].append(p["score"])

    for (d, h), score_list in scores_by_cell.items():
        if d in grid and h in grid[d]:
            grid[d][h]["count"] = len(score_list)
            grid[d][h]["avg_score"] = round(sum(score_list) / len(score_list), 4)

    return {
        "weekdays": weekdays,
        "hours": list(range(24)),
        "heatmap": grid,
        "total_analyzed_posts": len(posts),
    }

