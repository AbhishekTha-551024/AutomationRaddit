"""
Unit and integration tests for Discord Adapter.
"""

from unittest.mock import patch, MagicMock
import httpx
from starlette.testclient import TestClient
from app.adapters.discord import DiscordAdapter
from app.adapters.base import PublishResult
from app.main import app


def test_discord_adapter_missing_url():
    adapter = DiscordAdapter(webhook_url="")
    result = adapter.publish({"title": "Test", "body": "Hello"})
    assert result.success is False
    assert result.retryable is False
    assert result.error_message == "Discord webhook URL is not configured"


def test_discord_adapter_character_limit_truncation():
    adapter = DiscordAdapter(webhook_url="https://discord.com/api/webhooks/dummy/dummy")

    long_body = "A" * 2500
    mock_resp = MagicMock(status_code=200, json=lambda: {"id": "12345"})

    with patch("httpx.Client.post", return_value=mock_resp) as mock_post:
        result = adapter.publish({"title": "Long Post", "body": long_body})
        assert result.success is True
        # Check posted payload content length
        call_kwargs = mock_post.call_args[1]
        posted_content = call_kwargs["json"]["content"]
        assert len(posted_content) == 1993  # 1990 + "..."
        assert posted_content.endswith("...")


def test_discord_adapter_success():
    adapter = DiscordAdapter(webhook_url="https://discord.com/api/webhooks/dummy/dummy")
    mock_resp = MagicMock(status_code=200, json=lambda: {"id": "99887766"})

    with patch("httpx.Client.post", return_value=mock_resp):
        result = adapter.publish({"title": "Hello", "body": "World"})

    assert result.success is True
    assert result.external_post_id == "99887766"


def test_discord_adapter_permanent_errors_and_masking():
    fake_secret_url = "https://discord.com/api/webhooks/123/SUPER_SECRET_TOKEN"
    adapter = DiscordAdapter(webhook_url=fake_secret_url)

    # 404 webhook deleted / not found
    mock_404 = MagicMock(status_code=404, json=lambda: {"message": "Unknown Webhook"})
    with patch("httpx.Client.post", return_value=mock_404):
        result = adapter.publish({"body": "Test"})
        assert result.success is False
        assert result.retryable is False
        assert "SUPER_SECRET_TOKEN" not in result.error_message
        assert "Discord webhook not found (404)" in result.error_message


def test_discord_adapter_transient_errors():
    adapter = DiscordAdapter(webhook_url="https://discord.com/api/webhooks/dummy/dummy")

    # 429 Rate limit
    mock_429 = MagicMock(status_code=429, json=lambda: {"message": "You are being rate limited."})
    with patch("httpx.Client.post", return_value=mock_429):
        result = adapter.publish({"body": "Test"})
        assert result.success is False
        assert result.retryable is True

    # Timeout
    with patch("httpx.Client.post", side_effect=httpx.TimeoutException("Timed out")):
        result = adapter.publish({"body": "Test"})
        assert result.success is False
        assert result.retryable is True
        assert "timed out" in result.error_message.lower()


def test_dev_endpoint_discord():
    client = TestClient(app)

    with patch("app.adapters.discord.DiscordAdapter.publish") as mock_pub:
        mock_pub.return_value = PublishResult(success=True, external_post_id="555", error_message=None, retryable=True)

        resp = client.post("/dev/test-discord", json={"title": "Dev Post", "body": "Test content"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["external_post_id"] == "555"


def test_posts_api_validates_platform():
    client = TestClient(app)

    # 1. Valid platform: discord
    valid_payload = {
        "title": "Discord API Test",
        "body": "Testing Discord support in POST /posts",
        "targets": [{"platform": "discord", "scheduled_at": "2026-10-30T12:00:00"}],
    }
    resp = client.post("/posts", json=valid_payload)
    assert resp.status_code == 201
    assert resp.json()["targets"][0]["platform"] == "discord"

    # 2. Invalid platform: unsupported_xyz -> returns 422
    invalid_payload = {
        "title": "Invalid Test",
        "body": "Testing 422 error",
        "targets": [{"platform": "unsupported_xyz", "scheduled_at": "2026-10-30T12:00:00"}],
    }
    resp_invalid = client.post("/posts", json=invalid_payload)
    assert resp_invalid.status_code == 422

