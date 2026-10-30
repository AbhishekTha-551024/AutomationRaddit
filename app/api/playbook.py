"""
Playbook API router.
Provides CRUD operations for content guidelines and export of active rules.
"""

from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query, Body, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.playbook import PlaybookRule
from app.services.playbook_service import (
    list_rules,
    create_rule,
    update_rule_status,
    update_rule_details,
    delete_rule,
    get_active_playbook_text,
)

router = APIRouter(prefix="/playbook", tags=["Playbook"])


class RuleCreateRequest(BaseModel):
    rule_text: str = Field(..., min_length=3, description="The content guideline or rule")
    category: str = Field("hook", description="Category: hook, format, timing, length, topic, tone, avoid")
    confidence: str = Field("medium", description="Confidence rating: low, medium, high")
    evidence: str = Field("", description="Supporting evidence or sample size notes")
    status: str = Field("active", description="Default status for manual rules: active or suggested")


class RuleUpdateRequest(BaseModel):
    status: Optional[str] = Field(None, description="New status: active, suggested, rejected")
    rule_text: Optional[str] = None
    category: Optional[str] = None
    evidence: Optional[str] = None


@router.get("/rules")
def get_rules(
    status: Optional[str] = Query(None, description="Filter by status: active, suggested, rejected"),
    db: Session = Depends(get_db),
):
    """Lists Playbook rules with optional status filter."""
    rules = list_rules(db, status=status)
    return [
        {
            "id": r.id,
            "rule_text": r.rule_text,
            "category": r.category,
            "source": r.source,
            "confidence": r.confidence,
            "evidence": r.evidence,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rules
    ]


@router.post("/rules")
def create_manual_rule(
    payload: RuleCreateRequest,
    db: Session = Depends(get_db),
):
    """Creates a new manual Playbook rule."""
    rule = create_rule(
        db=db,
        rule_text=payload.rule_text,
        category=payload.category,
        source="manual",
        confidence=payload.confidence,
        evidence=payload.evidence,
        status=payload.status,
    )
    return {
        "status": "success",
        "rule": {
            "id": rule.id,
            "rule_text": rule.rule_text,
            "category": rule.category,
            "status": rule.status,
        },
    }


@router.patch("/rules/{rule_id}")
def update_existing_rule(
    rule_id: int,
    payload: RuleUpdateRequest,
    db: Session = Depends(get_db),
):
    """Updates rule status (activate, reject) or rule text/category."""
    if payload.status:
        updated = update_rule_status(db, rule_id, payload.status)
        if not updated:
            raise HTTPException(status_code=404, detail="Rule not found")
        return {"status": "success", "rule_id": rule_id, "new_status": updated.status}

    updated = update_rule_details(
        db,
        rule_id,
        rule_text=payload.rule_text,
        category=payload.category,
        evidence=payload.evidence,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"status": "success", "rule_id": rule_id}


@router.delete("/rules/{rule_id}")
def delete_existing_rule(
    rule_id: int,
    db: Session = Depends(get_db),
):
    """Permanently deletes a Playbook rule."""
    success = delete_rule(db, rule_id)
    if not success:
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"status": "success", "deleted_id": rule_id}


@router.get("/export")
def export_active_playbook(db: Session = Depends(get_db)):
    """Exports all active, approved Playbook rules as a compact markdown text block."""
    text_content = get_active_playbook_text(db)
    return Response(content=text_content, media_type="text/plain; charset=utf-8")

