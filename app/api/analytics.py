"""
Analytics API endpoints.
Provides structured JSON feeds for post-level performance, aggregate summaries,
timing heatmaps, and content feature patterns.
"""

from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.database import get_db
from app.services.analytics import (
    get_scored_posts,
    get_analytics_summary,
    get_best_times_heatmap,
)
from app.services.patterns import analyze_patterns

router = APIRouter(prefix="/analytics", tags=["Analytics"])


@router.get("/posts")
def list_scored_posts(
    days: int = Query(30, ge=1, le=365, description="Lookback window in days"),
    db: Session = Depends(get_db),
):
    """Returns all Threads posts within lookback window with their latest metrics, score, and vs-average ratio."""
    return get_scored_posts(db, days=days)


@router.get("/summary")
def get_summary_metrics(
    days: int = Query(30, ge=1, le=365, description="Lookback window in days"),
    db: Session = Depends(get_db),
):
    """Returns aggregate views, likes, replies, reposts, average score, top 5, bottom 5, and follower growth."""
    return get_analytics_summary(db, days=days)


@router.get("/best-times")
def get_best_times(
    days: int = Query(60, ge=1, le=365, description="Lookback window in days"),
    db: Session = Depends(get_db),
):
    """Returns post counts and average scores broken down by weekday and hour in IST."""
    return get_best_times_heatmap(db, days=days)


@router.get("/patterns")
def get_content_patterns(
    days: int = Query(90, ge=1, le=365, description="Lookback window in days"),
    db: Session = Depends(get_db),
):
    """Returns engagement distributions across format, hook types, links, emojis, languages, and length."""
    return analyze_patterns(db, days=days)

