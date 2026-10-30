"""
SQLAlchemy model for execution and audit logs.
Tracks every event, attempt, and error for each post target.
"""

from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, Text, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class Log(Base):
    """
    The Log model records granular, immutable audit events for a scheduled target
    or system operational actions (such as credential updates).
    """
    __tablename__ = "logs"

    # Primary key identifier for the log entry
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Reference to the specific post target this log pertains to (nullable for system/platform logs)
    post_target_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("post_targets.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Severity level: "INFO", "WARNING", "ERROR", "DEBUG"
    level: Mapped[str] = mapped_column(String(20), nullable=False)

    # Detailed message describing the action or error (secrets must be redacted)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    # Timestamp when this log entry was recorded (always stored in UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )

    # Many-to-one relationship back to the associated post target
    post_target: Mapped["PostTarget"] = relationship(
        "PostTarget",
        back_populates="logs",
    )

