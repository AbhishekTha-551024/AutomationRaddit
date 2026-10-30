"""
SQLAlchemy models for metrics snapshots.
Tracks post-level engagement snapshots over time and daily account-level aggregates.
"""

from datetime import datetime, date, timezone
from typing import Optional
from sqlalchemy import String, Text, Integer, DateTime, Date, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class MetricSnapshot(Base):
    """
    Periodic engagement snapshot for an individual post target (or part of a thread chain).
    Captured at defined age intervals: 1h, 6h, 24h, 72h, 7d.
    """
    __tablename__ = "metric_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Reference to the post target
    post_target_id: Mapped[int] = mapped_column(
        ForeignKey("post_targets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Platform identifier (e.g. "threads")
    platform: Mapped[str] = mapped_column(String(50), nullable=False)

    # Part index for multi-part thread chains (0 for root/first post)
    part_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # External platform media ID (e.g. Threads media ID)
    external_media_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # Age bucket label: "1h", "6h", "24h", "72h", "7d"
    age_label: Mapped[str] = mapped_column(String(10), nullable=False)

    # Timestamp when this snapshot was fetched (UTC)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )

    # Engagement metrics (all nullable in case platform returns partial metrics)
    views: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    likes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    replies: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reposts: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    quotes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    shares: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Raw API response without sensitive tokens/secrets
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)

    # Relationship back to post target
    post_target: Mapped["PostTarget"] = relationship(
        "PostTarget",
        back_populates="metric_snapshots",
    )

    __table_args__ = (
        UniqueConstraint(
            "post_target_id",
            "part_index",
            "age_label",
            name="uq_metric_target_part_age",
        ),
    )


class AccountSnapshot(Base):
    """
    Daily snapshot of high-level account performance and audience size.
    """
    __tablename__ = "account_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Reference to the account credentials row
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The calendar date this snapshot represents
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # Follower count (nullable for small accounts if withheld by platform)
    followers_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Aggregate account metrics
    views: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    likes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    replies: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reposts: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    quotes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    clicks: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # When this snapshot was fetched (UTC)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )

    # Relationship back to account
    account: Mapped["Account"] = relationship(
        "Account",
        back_populates="account_snapshots",
    )

    __table_args__ = (
        UniqueConstraint("account_id", "snapshot_date", name="uq_account_snapshot_date"),
    )
