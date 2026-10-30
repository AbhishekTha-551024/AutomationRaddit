"""
SQLAlchemy model for platform connection testing and operational status.
Tracks health, last test timestamp, result message, and identity for each platform.
"""

from datetime import datetime
from typing import Optional
from sqlalchemy import String, Text, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class ConnectionStatus(Base):
    """
    Persists safe connection health metadata and diagnostic results
    per social platform. Updated daily or upon manual tests.
    """
    __tablename__ = "connection_status"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Platform identifier (e.g., "telegram", "discord", "threads", "instagram")
    platform: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)

    # Optional foreign key to accounts table
    account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Timestamp of the most recent read-only connection test
    last_test_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Whether the last connection test succeeded
    last_test_ok: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Diagnostic message or failure reason (plain text, never secrets)
    last_test_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Timestamp of the last successful publication or connection test
    last_success_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Public identity summary (e.g. "@mychannel", "username", "Discord Webhook")
    account_identity: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Relationship to account
    account = relationship("Account", backref="connection_status", lazy="joined")

