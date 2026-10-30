"""
SQLAlchemy models for AI Analyst execution logs and reports.
Tracks token consumption, estimated spend against monthly budget, and synthesized findings.
"""

from datetime import datetime, timezone
from sqlalchemy import String, Text, Integer, Float, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class UsageLog(Base):
    """
    Granular audit log tracking AI API requests, tokens, and estimated financial cost.
    Used for enforcing AI_MONTHLY_BUDGET_USD before making API calls.
    """
    __tablename__ = "usage_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Feature identifier: e.g. "analyst"
    feature: Mapped[str] = mapped_column(String(50), default="analyst", nullable=False, index=True)

    # Model name invoked (e.g. "grok-2", "grok-beta")
    model: Mapped[str] = mapped_column(String(100), nullable=False)

    # Token counts reported by provider API
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Estimated cost in USD
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # Timestamp of API execution (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )


class AnalysisReport(Base):
    """
    Weekly synthesized intelligence report produced by the AI Analyst.
    """
    __tablename__ = "analysis_reports"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Time of report generation (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )

    # Lookback window covered in days (e.g. 30)
    period_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)

    # Executive narrative summary
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)

    # Full structured findings and experiment suggestions as JSON text
    findings_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

    # Model used to generate report
    model: Mapped[str] = mapped_column(String(100), nullable=False)

    # Total cost of this analysis run
    cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

