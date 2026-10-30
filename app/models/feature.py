"""
SQLAlchemy model for rule-based post feature tagging.
Captures structural, linguistic, timing, and formatting attributes of published posts.
"""

from typing import Optional
from sqlalchemy import String, Text, Integer, Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class PostFeature(Base):
    """
    Feature metrics extracted from post content and scheduling parameters.
    One row per target per chain part (0 for single/root posts).
    """
    __tablename__ = "post_features"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Reference to the specific post target
    post_target_id: Mapped[int] = mapped_column(
        ForeignKey("post_targets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Part index for multi-part threads (0 for first/single part)
    part_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Content length metrics
    char_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    part_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # Structural format: "single", "thread", "question", "list", "story", "announcement"
    format: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # Opening line hook category: "question", "number", "bold_claim", "story_opening", "how_to", "other"
    hook_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # Content elements
    has_link: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    has_emoji: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    emoji_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    hashtag_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Language classification: "hinglish", "english", "hindi", "other"
    language_guess: Mapped[str] = mapped_column(String(30), default="english", nullable=False, index=True)

    # Publication timing in IST
    posted_hour_ist: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    posted_weekday_ist: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    # Optional topic tag (filled by AI analyst or manual user assignment)
    topic_tag: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Relationship back to post target
    post_target: Mapped["PostTarget"] = relationship(
        "PostTarget",
        back_populates="features",
    )

    __table_args__ = (
        UniqueConstraint(
            "post_target_id",
            "part_index",
            name="uq_feature_target_part",
        ),
    )

