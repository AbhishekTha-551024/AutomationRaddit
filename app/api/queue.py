"""
Queue Control API router.
Supports global pause and resume of scheduled post publishing.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.database import get_db
from app.services.queue_service import (
    is_publishing_paused,
    pause_publishing,
    resume_publishing,
)

router = APIRouter(prefix="/queue", tags=["Queue Controls"])


@router.get("/status")
def get_queue_status(db: Session = Depends(get_db)):
    """Check whether publishing is currently paused."""
    return {"paused": is_publishing_paused(db)}


@router.post("/pause")
def pause_queue(db: Session = Depends(get_db)):
    """
    Globally pauses post publishing.
    Scheduled posts will wait in pending status and will not be dispatched.
    """
    pause_publishing(db)
    return {
        "paused": True,
        "message": "Publishing is paused. Scheduled posts will wait.",
    }


@router.post("/resume")
def resume_queue(db: Session = Depends(get_db)):
    """
    Resumes post publishing.
    Overdue posts are automatically staggered 60 seconds apart per platform.
    """
    resume_publishing(db)
    return {
        "paused": False,
        "message": "Publishing resumed. Overdue posts were staggered.",
    }

