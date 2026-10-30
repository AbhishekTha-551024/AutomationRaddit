"""
Unit and integration tests for Phase 3:
Scheduler, Atomic Poller, Startup Recovery, and Posts API.
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient
from app.database import Base
from app.models import Post, PostTarget, Log, TargetStatus
from app.adapters.registry import get_adapter
from app.adapters.base import PublishResult
from app.adapters.telegram import TelegramAdapter
from app.api.posts import parse_to_utc, format_to_ist
from app.services.publisher import run_due_posts
from app.scheduler.scheduler import recover_orphaned_jobs
from app.main import app


def test_registry():
    adapter = get_adapter("telegram")
    assert isinstance(adapter, TelegramAdapter)

    try:
        get_adapter("unknown_platform")
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "Unsupported platform" in str(e)


def test_timezone_helpers():
    # Naive string assumed IST
    utc_dt = parse_to_utc("2026-10-30T10:00:00")
    assert utc_dt.tzinfo == timezone.utc
    assert utc_dt.hour == 4
    assert utc_dt.minute == 30

    # Display back in IST
    ist_str = format_to_ist(utc_dt)
    assert "2026-10-30 10:00:00 IST" in ist_str


def test_run_due_posts_and_logs():
    test_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=test_engine)
    TestingSession = sessionmaker(bind=test_engine)
    db = TestingSession()

    now_utc = datetime.now(timezone.utc)

    # 1. Post due in the past
    due_post = Post(title="Due Post", body="Should publish now")
    db.add(due_post)
    db.flush()

    due_target = PostTarget(
        post_id=due_post.id,
        platform="telegram",
        scheduled_at=now_utc - timedelta(minutes=5),
        status=TargetStatus.pending,
    )
    db.add(due_target)

    # 2. Post in the future (not due)
    future_post = Post(title="Future Post", body="Wait for later")
    db.add(future_post)
    db.flush()

    future_target = PostTarget(
        post_id=future_post.id,
        platform="telegram",
        scheduled_at=now_utc + timedelta(hours=2),
        status=TargetStatus.pending,
    )
    db.add(future_target)
    db.commit()

    # Mock adapter publish to succeed
    with patch.object(TelegramAdapter, "publish", return_value=PublishResult(success=True, external_post_id="999")):
        count = run_due_posts(db)

    assert count == 1

    # Verify due target is posted
    db.refresh(due_target)
    assert due_target.status == TargetStatus.posted
    assert due_target.external_post_id == "999"

    # Verify future target remains pending
    db.refresh(future_target)
    assert future_target.status == TargetStatus.pending

    # Verify logs created for due target
    logs = db.scalars(select(Log).where(Log.post_target_id == due_target.id)).all()
    assert len(logs) >= 2  # claimed + posted
    assert any("claimed" in l.message.lower() for l in logs)
    assert any("successfully published" in l.message.lower() for l in logs)

    db.close()


def test_startup_recovery():
    test_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=test_engine)
    TestingSession = sessionmaker(bind=test_engine)

    # Insert a target stuck in processing
    with TestingSession() as db:
        post = Post(body="Recovery test")
        db.add(post)
        db.flush()

        stuck_target = PostTarget(
            post_id=post.id,
            platform="telegram",
            scheduled_at=datetime.now(timezone.utc),
            status=TargetStatus.processing,
        )
        db.add(stuck_target)
        db.commit()
        target_id = stuck_target.id

    # Run recovery with SessionLocal pointed to TestingSession
    with patch("app.scheduler.scheduler.SessionLocal", side_effect=TestingSession):
        recover_orphaned_jobs()

    # Inspect in a new session
    with TestingSession() as db:
        target = db.get(PostTarget, target_id)
        assert target.status == TargetStatus.pending

        logs = db.scalars(select(Log).where(Log.post_target_id == target_id)).all()
        assert len(logs) == 1
        assert "recovered" in logs[0].message.lower()


def test_posts_api():
    client = TestClient(app)

    # 1. Create a post with target
    payload = {
        "title": "API Test",
        "body": "Hello world from API test",
        "targets": [
            {
                "platform": "telegram",
                "scheduled_at": "2026-10-30T15:30:00",
            }
        ],
    }

    create_resp = client.post("/posts", json=payload)
    assert create_resp.status_code == 201
    created_data = create_resp.json()
    assert created_data["title"] == "API Test"
    assert len(created_data["targets"]) == 1
    assert created_data["targets"][0]["status"] == "pending"
    assert "IST" in created_data["targets"][0]["scheduled_at_ist"]

    # 2. List posts
    list_resp = client.get("/posts")
    assert list_resp.status_code == 200
    posts_list = list_resp.json()
    assert len(posts_list) >= 1
