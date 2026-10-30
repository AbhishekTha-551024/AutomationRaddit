"""
SQLAlchemy model for platform accounts.
Stores encrypted API credentials and authentication metadata.
"""

from datetime import datetime
from typing import List, Optional
from sqlalchemy import String, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class Account(Base):
    """
    The Account model holds credentials and authentication details
    for a specific social platform (e.g., Telegram bot, Reddit account).
    """
    __tablename__ = "accounts"

    # Primary key identifier for the account
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Social platform name (e.g. "telegram", "reddit", "instagram")
    platform: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # Human-friendly label (e.g. "My Tech Channel Bot", "Reddit Personal")
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    # Securely encrypted credentials JSON (API keys, secrets, tokens)
    encrypted_credentials: Mapped[str] = mapped_column(Text, nullable=False)

    # Optional expiration datetime for access tokens requiring refresh (in UTC)
    token_expiry: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # One-to-many relationship: One account can be used by multiple post targets
    post_targets: Mapped[List["PostTarget"]] = relationship(
        "PostTarget",
        back_populates="account",
    )
    account_snapshots: Mapped[List["AccountSnapshot"]] = relationship(
        "AccountSnapshot",
        back_populates="account",
        cascade="all, delete-orphan",
    )

