"""
Unit and integration tests for Meta Threads Adapter, token lifecycle, and dev endpoints.
"""

import unittest
from datetime import datetime, timezone, timedelta
import json
from unittest.mock import patch, MagicMock
from cryptography.fernet import Fernet
import httpx
from starlette.testclient import TestClient
from app.adapters.threads import ThreadsAdapter, split_thread_parts
from app.adapters.base import PublishResult
from app.services.crypto_service import encrypt_string, decrypt_string
from app.services.threads_token_service import mask_secret
from app.models.account import Account
from app.models.target import PostTarget, TargetStatus
from app.models.post import Post
from app.main import app

# Generate a valid test Fernet key for the test session
TEST_KEY = Fernet.generate_key().decode()


class ThreadsAdapterTestCase(unittest.TestCase):

    def test_split_thread_parts(self):
        # 1. Single part without title
        parts = split_thread_parts(None, "Hello world")
        self.assertEqual(parts, ["Hello world"])

        # 2. Single part with title
        parts = split_thread_parts("Big News", "Hello world")
        self.assertEqual(parts, ["Big News\n\nHello world"])

        # 3. Multi-part thread chain
        raw_body = "Part 1 line 1\nPart 1 line 2\n---\nPart 2 line 1\n---\nPart 3 line 1"
        parts = split_thread_parts("Title", raw_body)
        self.assertEqual(len(parts), 3)
        self.assertEqual(parts[0], "Title\n\nPart 1 line 1\nPart 1 line 2")
        self.assertEqual(parts[1], "Part 2 line 1")
        self.assertEqual(parts[2], "Part 3 line 1")

    def test_crypto_service_roundtrip(self):
        with patch("app.services.crypto_service.settings.ENCRYPTION_KEY", TEST_KEY):
            secret = json.dumps({"access_token": "th_secret_token_123", "user_id": "999"})
            encrypted = encrypt_string(secret)
            self.assertNotEqual(encrypted, secret)
            decrypted = decrypt_string(encrypted)
            self.assertEqual(decrypted, secret)

    def test_mask_secret(self):
        secret_token = "TH_SECRET_TOKEN_XYZ_999"
        secret_app = "APP_SECRET_777"
        raw_msg = f"Failed to connect to Threads with token {secret_token} and secret {secret_app}"
        masked = mask_secret(raw_msg, secret_token, secret_app)
        self.assertNotIn(secret_token, masked)
        self.assertNotIn(secret_app, masked)
        self.assertIn("[REDACTED]", masked)

    def test_threads_500_char_limit_validation(self):
        part1 = "Part 1 content"
        part2 = "B" * 501
        body = f"{part1}\n---\n{part2}"

        mock_client = MagicMock(spec=httpx.Client)
        adapter = ThreadsAdapter(client=mock_client)

        with patch("app.adapters.threads.ensure_fresh_token", return_value={"access_token": "tok", "user_id": "u1"}):
            result = adapter.publish({"title": None, "body": body})
            self.assertFalse(result.success)
            self.assertFalse(result.retryable)
            self.assertIn("Part 2 is 501 characters, Threads allows 500", result.error_message)
            self.assertEqual(mock_client.post.call_count, 0)

    def test_threads_publish_single_post_success(self):
        mock_client = MagicMock(spec=httpx.Client)

        resp_create = MagicMock(status_code=200, json=lambda: {"id": "c_123"})
        resp_poll = MagicMock(status_code=200, json=lambda: {"status": "FINISHED"})
        resp_publish = MagicMock(status_code=200, json=lambda: {"id": "th_post_999"})

        mock_client.post.side_effect = [resp_create, resp_publish]
        mock_client.get.return_value = resp_poll

        adapter = ThreadsAdapter(client=mock_client)

        with patch("app.adapters.threads.ensure_fresh_token", return_value={"access_token": "test_tok", "user_id": "user_42"}):
            result = adapter.publish({"title": "Headline", "body": "My first Threads update!"})
            self.assertTrue(result.success)
            self.assertEqual(result.external_post_id, "th_post_999")

            create_call = mock_client.post.call_args_list[0]
            self.assertEqual(create_call[0][0], "https://graph.threads.net/v1.0/user_42/threads")
            self.assertEqual(create_call[1]["data"]["text"], "Headline\n\nMy first Threads update!")
            self.assertEqual(create_call[1]["data"]["media_type"], "TEXT")

            pub_call = mock_client.post.call_args_list[1]
            self.assertEqual(pub_call[0][0], "https://graph.threads.net/v1.0/user_42/threads_publish")
            self.assertEqual(pub_call[1]["data"]["creation_id"], "c_123")

    def test_threads_publish_thread_chain_success(self):
        mock_client = MagicMock(spec=httpx.Client)

        c1_create = MagicMock(status_code=200, json=lambda: {"id": "c1"})
        p1_pub = MagicMock(status_code=200, json=lambda: {"id": "p1"})
        c2_create = MagicMock(status_code=200, json=lambda: {"id": "c2"})
        p2_pub = MagicMock(status_code=200, json=lambda: {"id": "p2"})

        mock_client.post.side_effect = [c1_create, p1_pub, c2_create, p2_pub]
        mock_client.get.return_value = MagicMock(status_code=200, json=lambda: {"status": "FINISHED"})

        adapter = ThreadsAdapter(client=mock_client)

        body = "First post in thread\n---\nSecond post replying"
        with patch("app.adapters.threads.ensure_fresh_token", return_value={"access_token": "test_tok", "user_id": "u1"}), \
             patch("time.sleep", return_value=None):
            result = adapter.publish({"title": None, "body": body})
            self.assertTrue(result.success)
            self.assertEqual(result.external_post_id, "p1")

            part2_create_call = mock_client.post.call_args_list[2]
            self.assertEqual(part2_create_call[1]["data"]["reply_to_id"], "p1")
            self.assertEqual(part2_create_call[1]["data"]["text"], "Second post replying")

    def test_threads_idempotent_retry(self):
        mock_client = MagicMock(spec=httpx.Client)

        c2_create = MagicMock(status_code=200, json=lambda: {"id": "c2"})
        p2_pub = MagicMock(status_code=200, json=lambda: {"id": "p2"})

        mock_client.post.side_effect = [c2_create, p2_pub]
        mock_client.get.return_value = MagicMock(status_code=200, json=lambda: {"status": "FINISHED"})

        adapter = ThreadsAdapter(client=mock_client)

        body = "Part 1 (already done)\n---\nPart 2 (needs publish)"
        mock_target = MagicMock()
        mock_target.publish_state = json.dumps({"published_part_ids": ["p1"]})
        mock_target.id = 10

        with patch("app.adapters.threads.ensure_fresh_token", return_value={"access_token": "tok", "user_id": "u1"}), \
             patch("time.sleep", return_value=None):
            result = adapter.publish({"title": None, "body": body}, target=mock_target)
            self.assertTrue(result.success)
            self.assertEqual(result.external_post_id, "p1")
            self.assertEqual(mock_client.post.call_count, 2)
            self.assertEqual(mock_client.post.call_args_list[0][1]["data"]["reply_to_id"], "p1")

    def test_threads_permanent_and_transient_errors(self):
        mock_client = MagicMock(spec=httpx.Client)

        # Permanent: 401 Unauthorized
        resp_401 = MagicMock(status_code=401, json=lambda: {"error": {"message": "Invalid OAuth token."}})
        mock_client.post.return_value = resp_401

        adapter = ThreadsAdapter(client=mock_client)
        with patch("app.adapters.threads.ensure_fresh_token", return_value={"access_token": "SECRET_TOK", "user_id": "u1"}):
            result = adapter.publish({"body": "Hello"})
            self.assertFalse(result.success)
            self.assertFalse(result.retryable)
            self.assertNotIn("SECRET_TOK", result.error_message)
            self.assertIn("401", result.error_message)

        # Transient: 429 Rate limit
        resp_429 = MagicMock(status_code=429, json=lambda: {"error": {"message": "User has exceeded rate limit"}})
        mock_client.post.return_value = resp_429
        with patch("app.adapters.threads.ensure_fresh_token", return_value={"access_token": "tok", "user_id": "u1"}):
            result = adapter.publish({"body": "Hello"})
            self.assertFalse(result.success)
            self.assertTrue(result.retryable)

    def test_dev_endpoints_threads(self):
        client = TestClient(app)

        with patch("app.adapters.threads.ThreadsAdapter.publish") as mock_pub:
            mock_pub.return_value = PublishResult(success=True, external_post_id="th_dev_123", retryable=True)
            resp = client.post("/dev/test-threads", json={"title": "Dev Threads", "body": "Dev content"})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["external_post_id"], "th_dev_123")

        resp_acc = client.get("/dev/threads-account")
        self.assertEqual(resp_acc.status_code, 200)
        data = resp_acc.json()
        self.assertIn("username", data)
        self.assertIn("token_expiry", data)
        self.assertIn("is_expired", data)
        self.assertNotIn("access_token", data)
        self.assertNotIn("client_secret", data)
        self.assertNotIn("encrypted_credentials", data)

    def test_posts_api_accepts_threads(self):
        client = TestClient(app)

        payload = {
            "title": "Threads Post Test",
            "body": "Cross-posting to Threads!",
            "targets": [{"platform": "threads", "scheduled_at": "2026-10-30T14:00:00"}],
        }
        resp = client.post("/posts", json=payload)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["targets"][0]["platform"], "threads")


if __name__ == "__main__":
    unittest.main()
