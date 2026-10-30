"""
SQLAlchemy model for social media posts.
Represents the core content (text, title, media) to be scheduled.
"""

from datetime import datetime, timezone
from typing import List, Optional
from sqlalchemy import String, Text, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class Post(Base):
    """
    The Post model stores the original post content.
    One post can be scheduled across multiple target platforms.
    """
    __tablename__ = "posts"

    # Primary key identifier for the post
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Optional headline or title (used by Reddit, or as header on Telegram/Instagram)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # The main message or caption body (required)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    # Optional local path to an attached image or video file
    media_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # Optional publicly accessible image or video URL (required for Instagram)
    media_url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)

    # Timestamp when this post was created in the system (always stored in UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )

    # Timestamp when post content was last edited (in UTC)
    edited_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Soft-delete timestamp (NULL means active, set timestamp means moved to Trash)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    # One-to-many relationship: A post can target multiple platforms
    # Deleting a post cascades and deletes all associated scheduled targets
    targets: Mapped[List["PostTarget"]] = relationship(
        "PostTarget",
        back_populates="post",
        cascade="all, delete-orphan",
    )

