"""
Unit and integration tests for Phase 2: Telegram Adapter.
"""

from unittest.mock import patch, MagicMock
import httpx
from starlette.testclient import TestClient
from app.adapters.base import PublishResult
from app.adapters.telegram import TelegramAdapter
from app.main import app


def test_publish_result_structure():
    res = PublishResult(success=True, external_post_id="12345", error_message=None)
    assert res.success is True
    assert res.external_post_id == "12345"
    assert res.error_message is None


def test_telegram_adapter_missing_credentials():
    adapter = TelegramAdapter(bot_token="", chat_id="")
    result = adapter.publish({"title": "Test", "body": "Hello"})
    assert result.success is False
    assert "credentials missing" in result.error_message.lower()


def test_telegram_adapter_success():
    fake_token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    fake_chat = "-1001234567890"

    adapter = TelegramAdapter(bot_token=fake_token, chat_id=fake_chat)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "ok": True,
        "result": {
            "message_id": 98765,
            "text": "Hello World",
        },
    }

    with patch("httpx.Client.post", return_value=mock_resp):
        result = adapter.publish({"title": "Notice", "body": "Hello World"})

    assert result.success is True
    assert result.external_post_id == "98765"
    assert result.error_message is None


def test_telegram_adapter_api_error_redacts_token():
    fake_token = "SECRET_TOKEN_DO_NOT_LEAK"
    fake_chat = "-1001234567890"

    adapter = TelegramAdapter(bot_token=fake_token, chat_id=fake_chat)

    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.json.return_value = {
        "ok": False,
        "error_code": 400,
        "description": "Bad Request: chat not found",
    }

    with patch("httpx.Client.post", return_value=mock_resp):
        result = adapter.publish({"title": "Test", "body": "Hello"})

    assert result.success is False
    assert "chat not found" in result.error_message
    assert fake_token not in result.error_message


def test_telegram_adapter_timeout_error():
    fake_token = "SECRET_TOKEN_DO_NOT_LEAK"
    fake_chat = "-1001234567890"

    adapter = TelegramAdapter(bot_token=fake_token, chat_id=fake_chat)

    with patch("httpx.Client.post", side_effect=httpx.TimeoutException("Connection timed out")):
        result = adapter.publish({"title": "Test", "body": "Hello"})

    assert result.success is False
    assert "timed out" in result.error_message.lower()
    assert fake_token not in result.error_message


def test_dev_endpoint_telegram():
    client = TestClient(app)

    # Test invoking dev endpoint
    with patch("app.adapters.telegram.TelegramAdapter.publish") as mock_pub:
        mock_pub.return_value = PublishResult(success=True, external_post_id="4242", error_message=None)

        resp = client.post("/dev/test-telegram", json={"title": "Dev Post", "body": "Testing Phase 2"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["external_post_id"] == "4242"
        assert data["error_message"] is None

