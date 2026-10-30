"""
Playbook rule lifecycle management service.
Handles storage, approval workflow (suggested -> active/rejected), and markdown export
of actionable content guidelines.
"""

from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models.playbook import PlaybookRule


def list_rules(db: Session, status: Optional[str] = None) -> List[PlaybookRule]:
    """
    Returns rules optionally filtered by status ('active', 'suggested', 'rejected').
    """
    stmt = select(PlaybookRule)
    if status:
        stmt = stmt.where(PlaybookRule.status == status)
    stmt = stmt.order_by(PlaybookRule.category.asc(), PlaybookRule.id.desc())
    return list(db.scalars(stmt).all())


def create_rule(
    db: Session,
    rule_text: str,
    category: str = "hook",
    source: str = "manual",
    confidence: str = "medium",
    evidence: str = "",
    status: str = "suggested",
) -> PlaybookRule:
    """
    Creates a new playbook rule.
    Rules from stats/AI must always default to status='suggested'.
    """
    clean_text = rule_text.strip()
    rule = PlaybookRule(
        rule_text=clean_text,
        category=category.lower().strip(),
        source=source.lower().strip(),
        confidence=confidence.lower().strip(),
        evidence=evidence.strip(),
        status=status.lower().strip(),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


def update_rule_status(db: Session, rule_id: int, new_status: str) -> Optional[PlaybookRule]:
    """
    Transitions a rule between 'suggested', 'active', or 'rejected'.
    Only explicit user action invokes this function.
    """
    rule = db.get(PlaybookRule, rule_id)
    if not rule:
        return None

    rule.status = new_status.lower().strip()
    rule.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(rule)
    return rule


def update_rule_details(
    db: Session,
    rule_id: int,
    rule_text: Optional[str] = None,
    category: Optional[str] = None,
    evidence: Optional[str] = None,
) -> Optional[PlaybookRule]:
    """Updates the content or categorization of an existing rule."""
    rule = db.get(PlaybookRule, rule_id)
    if not rule:
        return None

    if rule_text is not None:
        rule.rule_text = rule_text.strip()
    if category is not None:
        rule.category = category.lower().strip()
    if evidence is not None:
        rule.evidence = evidence.strip()

    rule.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(rule)
    return rule


def delete_rule(db: Session, rule_id: int) -> bool:
    """Deletes a rule by ID."""
    rule = db.get(PlaybookRule, rule_id)
    if not rule:
        return False
    db.delete(rule)
    db.commit()
    return True


def get_active_playbook_text(db: Session) -> str:
    """
    Renders all approved active rules into a compact markdown prompt block.
    Used for injection into AI composer/research workflows.
    """
    active_rules = list_rules(db, status="active")
    if not active_rules:
        return "# Content Playbook\n\nNo active rules have been approved yet."

    categories: Dict[str, List[PlaybookRule]] = {}
    for r in active_rules:
        cat = r.category.capitalize()
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(r)

    lines: List[str] = [
        "# Active Content Playbook Guidelines",
        "Follow these proven performance guidelines when planning and writing posts:",
        "",
    ]

    for cat_name, r_list in categories.items():
        lines.append(f"## {cat_name}")
        for r in r_list:
            ev = f" (Evidence: {r.evidence})" if r.evidence else ""
            lines.append(f"- [{r.confidence.upper()}] {r.rule_text}{ev}")
        lines.append("")

    return "\n".join(lines).strip()

