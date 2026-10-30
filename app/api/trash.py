"""
Trash API router.
Provides endpoints for permanent deletion of individual posts or targets,
and emptying all items currently marked as deleted.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.services.post_operations import (
    permanent_delete_post,
    permanent_delete_target,
    empty_trash,
)

router = APIRouter(prefix="/trash", tags=["Trash Management"])


@router.delete("/{item_id}/permanent")
def permanently_delete_item(
    item_id: int,
    item_type: str = Query("post", pattern="^(post|target)$"),
    db: Session = Depends(get_db),
):
    """
    Permanently deletes a post or target from the database,
    cascading cleanup of its metric snapshots, post features, and audit logs.
    """
    try:
        if item_type == "target":
            res = permanent_delete_target(db, target_id=item_id)
        else:
            res = permanent_delete_post(db, post_id=item_id)
        return res
    except ValueError as ve:
        err_msg = str(ve)
        if "not found" in err_msg.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=err_msg)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=err_msg)


@router.post("/empty")
def empty_all_trash(db: Session = Depends(get_db)):
    """
    Permanently purges all items currently marked as deleted in Trash.
    """
    purged_count = empty_trash(db)
    return {"status": "success", "purged_count": purged_count}

