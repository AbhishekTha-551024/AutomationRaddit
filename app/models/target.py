"""
SQLAlchemy model for scheduled target platforms.
Tracks individual platform dispatch status, retry attempts, and results.
"""

import enum
from datetime import datetime
from typing import List, Optional
from sqlalchemy import String, Text, Integer, DateTime, ForeignKey, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class TargetStatus(str, enum.Enum):
    """
    Lifecycle status of a scheduled target platform publication.
    - draft: created as a draft, not yet scheduled for dispatch
    - pending: scheduled and waiting for dispatch time
    - processing: currently being published by an adapter (locked)
    - posted: successfully published to platform
    - failed: publish failed after retries
    - cancelled: cancelled by user, can be restored
    """
    draft = "draft"
    pending = "pending"
    processing = "processing"
    posted = "posted"
    failed = "failed"
    cancelled = "cancelled"


class PostTarget(Base):
    """
    Represents an individual target platform destination for a post.
    For instance, a single post might have one target for Telegram and one for Reddit.
    """
    __tablename__ = "post_targets"

    # Primary key identifier for this target entry
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Reference to the parent post
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Platform to publish to (e.g. "telegram", "reddit", "instagram", "threads")
    platform: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # Optional foreign key to account credentials
    account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Scheduled broadcast datetime (always stored in UTC)
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    # Current lifecycle state (pending, processing, posted, failed)
    status: Mapped[TargetStatus] = mapped_column(
        SAEnum(TargetStatus, native_enum=False, values_callable=lambda x: [e.value for e in x]),
        default=TargetStatus.pending,
        nullable=False,
        index=True,
    )

    # Actual time when this target was successfully published to the platform (in UTC)
    posted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    # Number of dispatch attempts made so far
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Earliest time to re-attempt dispatch if a previous attempt failed (in UTC)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    # External ID or URL returned by the platform upon successful publication
    external_post_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Detailed error description if the last publish attempt failed
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # JSON-encoded publishing checkpoint for idempotent retries (e.g. list of published thread part IDs)
    publish_state: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Soft-delete timestamp (NULL means active, set timestamp means moved to Trash)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    # Relationships
    post: Mapped["Post"] = relationship("Post", back_populates="targets")
    account: Mapped[Optional["Account"]] = relationship("Account", back_populates="post_targets")
    logs: Mapped[List["Log"]] = relationship(
        "Log",
        back_populates="post_target",
        cascade="all, delete-orphan",
    )
    metric_snapshots: Mapped[List["MetricSnapshot"]] = relationship(
        "MetricSnapshot",
        back_populates="post_target",
        cascade="all, delete-orphan",
    )
    features: Mapped[List["PostFeature"]] = relationship(
        "PostFeature",
        back_populates="post_target",
        cascade="all, delete-orphan",
    )
