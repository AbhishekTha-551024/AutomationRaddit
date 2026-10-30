"""
SQLAlchemy model for Playbook content rules.
Maintains curated guidelines (hook style, formatting, timing, length) derived from stats, AI, or manual input.
"""

from datetime import datetime, timezone
from sqlalchemy import String, Text, Integer, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class PlaybookRule(Base):
    """
    Playbook rule representing an actionable content guideline.
    Rules proposed by stats or AI start as 'suggested' until approved by the user.
    """
    __tablename__ = "playbook_rules"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Human-readable rule text
    rule_text: Mapped[str] = mapped_column(Text, nullable=False)

    # Category: "hook", "format", "timing", "length", "topic", "tone", "avoid"
    category: Mapped[str] = mapped_column(String(50), default="hook", nullable=False, index=True)

    # Origin: "manual", "stats", "ai"
    source: Mapped[str] = mapped_column(String(30), default="manual", nullable=False, index=True)

    # Confidence rating: "low", "medium", "high"
    confidence: Mapped[str] = mapped_column(String(20), default="medium", nullable=False)

    # Evidence or rationale (e.g. "Posts with numbers had 2.4x higher engagement across 14 posts")
    evidence: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # Lifecycle status: "suggested", "active", "rejected"
    # ONLY the user can transition rules to "active" or "rejected"
    status: Mapped[str] = mapped_column(String(30), default="suggested", nullable=False, index=True)

    # Creation and update timestamps (always stored in UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

