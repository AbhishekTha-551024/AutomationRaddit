"""
SQLAlchemy model for alert delivery logs.
Tracks sent notification events, deduplication keys, cooldowns, and transmission status.
"""

from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import String, Text, Boolean, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class AlertLog(Base):
    """
    Records history of alerts dispatched to Telegram or queued.
    Prevents alert storms and provides an audit log for /connections or /settings.
    """
    __tablename__ = "alert_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Kind of alert (e.g., "failed_post", "token_expiring", "connection_failed", "paused_too_long", "missed_posts", "budget", "daily_digest")
    kind: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # Deduplication key to prevent repeating identical alerts within cooldown window
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)

    # When the alert was sent or recorded (UTC)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )

    # Whether the message successfully reached Telegram
    success: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Error description if transmission failed (without exposing tokens)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Sanitized preview of the alert text
    message_preview: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

