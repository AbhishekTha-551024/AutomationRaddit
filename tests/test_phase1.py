"""
Basic unit/smoke test for Phase 1.
Verifies that:
1. Settings load properly from .env
2. Base metadata contains all 4 tables (posts, post_targets, accounts, logs)
3. TargetStatus enum has expected members
4. Database tables can be created cleanly in an in-memory SQLite database
"""

from sqlalchemy import create_engine, inspect
from app.config import settings
from app.database import Base
from app.models import Post, PostTarget, Account, Log, TargetStatus


def test_settings_loaded():
    assert settings.DATABASE_URL is not None
    assert settings.APP_ENV is not None


def test_models_registered():
    tables = Base.metadata.tables.keys()
    assert "posts" in tables
    assert "post_targets" in tables
    assert "accounts" in tables
    assert "logs" in tables


def test_target_status_enum():
    assert TargetStatus.pending.value == "pending"
    assert TargetStatus.processing.value == "processing"
    assert TargetStatus.posted.value == "posted"
    assert TargetStatus.failed.value == "failed"


def test_in_memory_db_creation():
    test_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=test_engine)

    inspector = inspect(test_engine)
    table_names = inspector.get_table_names()

    assert "posts" in table_names
    assert "post_targets" in table_names
    assert "accounts" in table_names
    assert "logs" in table_names

    # Verify columns on posts table
    post_cols = [c["name"] for c in inspector.get_columns("posts")]
    assert "id" in post_cols
    assert "title" in post_cols
    assert "body" in post_cols
    assert "media_path" in post_cols
    assert "created_at" in post_cols

    # Verify columns on post_targets table
    target_cols = [c["name"] for c in inspector.get_columns("post_targets")]
    assert "id" in target_cols
    assert "post_id" in target_cols
    assert "platform" in target_cols
    assert "account_id" in target_cols
    assert "scheduled_at" in target_cols
    assert "status" in target_cols
    assert "attempts" in target_cols
    assert "next_retry_at" in target_cols
    assert "external_post_id" in target_cols
    assert "error_message" in target_cols

