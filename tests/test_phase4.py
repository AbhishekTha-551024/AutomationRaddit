"""
Unit and integration tests for Phase 4:
Retries with backoff, permanent error bypass, logs, and status endpoints.
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient
from app.config import Settings
from app.database import Base
from app.models import Post, PostTarget, Log, TargetStatus
from app.adapters.base import PublishResult
from app.adapters.telegram import TelegramAdapter
from app.services.publisher import run_due_posts
from app.main import app


def test_retry_delays_config():
    s = Settings(RETRY_DELAYS_MINUTES="1,2,3")
    assert s.retry_delays == [1, 2, 3]

    s_default = Settings(RETRY_DELAYS_MINUTES="5,15,60")
    assert s_default.retry_delays == [5, 15, 60]


def test_telegram_retryable_classification():
    adapter = TelegramAdapter(bot_token="fake", chat_id="fake")

    # 400 Bad Request (chat not found) -> permanent
    resp_400 = MagicMock(status_code=400, json=lambda: {"ok": False, "error_code": 400, "description": "Chat not found"})
    with patch("httpx.Client.post", return_value=resp_400):
        res = adapter.publish({"body": "hi"})
        assert res.success is False
        assert res.retryable is False

    # 403 Forbidden (bot blocked) -> permanent
    resp_403 = MagicMock(status_code=403, json=lambda: {"ok": False, "error_code": 403, "description": "Bot blocked"})
    with patch("httpx.Client.post", return_value=resp_403):
        res = adapter.publish({"body": "hi"})
        assert res.success is False
        assert res.retryable is False

    # 429 Too Many Requests -> retryable
    resp_429 = MagicMock(status_code=429, json=lambda: {"ok": False, "error_code": 429, "description": "Too many requests"})
    with patch("httpx.Client.post", return_value=resp_429):
        res = adapter.publish({"body": "hi"})
        assert res.success is False
        assert res.retryable is True


def test_retry_backoff_lifecycle():
    test_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=test_engine)
    TestingSession = sessionmaker(bind=test_engine)
    db = TestingSession()

    now_utc = datetime.now(timezone.utc)

    post = Post(title="Retry Test", body="Checking backoff")
    db.add(post)
    db.flush()

    target = PostTarget(
        post_id=post.id,
        platform="telegram",
        scheduled_at=now_utc - timedelta(minutes=1),
        status=TargetStatus.pending,
        attempts=0,
    )
    db.add(target)
    db.commit()

    retryable_failure = PublishResult(success=False, error_message="Network glitch", retryable=True)

    with patch("app.services.publisher.settings.RETRY_DELAYS_MINUTES", "1,2,3"):
        # Attempt 1
        with patch.object(TelegramAdapter, "publish", return_value=retryable_failure):
            count = run_due_posts(db)
        assert count == 1
        db.refresh(target)
        assert target.status == TargetStatus.pending
        assert target.attempts == 1
        assert target.next_retry_at is not None

        # Simulate time passing beyond next_retry_at
        target.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

        # Attempt 2
        with patch.object(TelegramAdapter, "publish", return_value=retryable_failure):
            run_due_posts(db)
        db.refresh(target)
        assert target.status == TargetStatus.pending
        assert target.attempts == 2

        # Simulate time passing beyond next_retry_at
        target.next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

        # Attempt 3 (Terminal failure)
        with patch.object(TelegramAdapter, "publish", return_value=retryable_failure):
            run_due_posts(db)
        db.refresh(target)
        assert target.status == TargetStatus.failed
        assert target.attempts == 3
        assert target.next_retry_at is None

    # Verify logs captured for all 3 attempts
    logs = db.scalars(select(Log).where(Log.post_target_id == target.id)).all()
    assert len(logs) >= 6  # 3 claims + 2 retry scheduled warnings + 1 gave up error
    assert any("Maximum retries exceeded" in l.message for l in logs)

    db.close()


def test_permanent_error_immediately_fails():
    test_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=test_engine)
    TestingSession = sessionmaker(bind=test_engine)
    db = TestingSession()

    now_utc = datetime.now(timezone.utc)
    post = Post(title="Permanent Test", body="Wrong credentials")
    db.add(post)
    db.flush()

    target = PostTarget(
        post_id=post.id,
        platform="telegram",
        scheduled_at=now_utc - timedelta(minutes=1),
        status=TargetStatus.pending,
        attempts=0,
    )
    db.add(target)
    db.commit()

    perm_failure = PublishResult(success=False, error_message="Chat not found", retryable=False)

    with patch.object(TelegramAdapter, "publish", return_value=perm_failure):
        run_due_posts(db)

    db.refresh(target)
    assert target.status == TargetStatus.failed
    assert target.attempts == 1
    assert target.next_retry_at is None

    logs = db.scalars(select(Log).where(Log.post_target_id == target.id)).all()
    assert any("Permanent error" in l.message for l in logs)

    db.close()


def test_status_and_logs_endpoints():
    client = TestClient(app)

    # 1. Create a post
    payload = {
        "title": "Endpoints Test",
        "body": "Checking endpoints in Phase 4",
        "targets": [{"platform": "telegram", "scheduled_at": "2026-10-30T10:00:00"}],
    }
    create_resp = client.post("/posts", json=payload)
    assert create_resp.status_code == 201
    post_data = create_resp.json()
    post_id = post_data["id"]
    target_id = post_data["targets"][0]["id"]

    # 2. GET /posts/{post_id}
    get_post_resp = client.get(f"/posts/{post_id}")
    assert get_post_resp.status_code == 200
    assert get_post_resp.json()["id"] == post_id

    # 3. GET /targets/{target_id}/logs
    logs_resp = client.get(f"/targets/{target_id}/logs")
    assert logs_resp.status_code == 200

    # 4. POST /targets/{target_id}/retry when NOT failed -> expect 400
    retry_resp = client.post(f"/targets/{target_id}/retry")
    assert retry_resp.status_code == 400

    # 5. GET /logs
    all_logs_resp = client.get("/logs?limit=10")
    assert all_logs_resp.status_code == 200

    # 6. GET /stats
    stats_resp = client.get("/stats")
    assert stats_resp.status_code == 200
    stats_data = stats_resp.json()
    assert "pending" in stats_data
    assert "total" in stats_data
