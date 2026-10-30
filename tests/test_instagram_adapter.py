"""
Unit and integration tests for Meta Instagram Graph API Adapter and dev endpoints.
"""

import unittest
from unittest.mock import patch, MagicMock
import httpx
from starlette.testclient import TestClient
from app.adapters.instagram import InstagramAdapter
from app.adapters.base import PublishResult
from app.main import app


class InstagramAdapterTestCase(unittest.TestCase):

    def test_instagram_missing_media_url(self):
        mock_client = MagicMock(spec=httpx.Client)
        adapter = InstagramAdapter(client=mock_client)

        with patch("app.adapters.instagram.ensure_fresh_instagram_token", return_value={"access_token": "tok", "account_id": "12345"}):
            # Attempt to publish text-only post without media_url
            result = adapter.publish({"title": "No Image", "body": "Caption text"})
            self.assertFalse(result.success)
            self.assertFalse(result.retryable)
            self.assertIn("requires a public image or video URL", result.error_message)
            self.assertEqual(mock_client.post.call_count, 0)

    def test_instagram_invalid_media_url_scheme(self):
        mock_client = MagicMock(spec=httpx.Client)
        adapter = InstagramAdapter(client=mock_client)

        with patch("app.adapters.instagram.ensure_fresh_instagram_token", return_value={"access_token": "tok", "account_id": "12345"}):
            # Local file path instead of public HTTP/HTTPS URL
            result = adapter.publish({"title": "Local file", "body": "Caption", "media_url": "C:/images/pic.jpg"})
            self.assertFalse(result.success)
            self.assertFalse(result.retryable)
            self.assertIn("must be a publicly accessible HTTP/HTTPS URL", result.error_message)
            self.assertEqual(mock_client.post.call_count, 0)

    def test_instagram_caption_length_validation(self):
        mock_client = MagicMock(spec=httpx.Client)
        adapter = InstagramAdapter(client=mock_client)

        long_caption = "X" * 2205
        with patch("app.adapters.instagram.ensure_fresh_instagram_token", return_value={"access_token": "tok", "account_id": "12345"}):
            result = adapter.publish({"title": None, "body": long_caption, "media_url": "https://example.com/img.jpg"})
            self.assertFalse(result.success)
            self.assertFalse(result.retryable)
            self.assertIn("Caption exceeds Instagram's 2,200 character limit", result.error_message)
            self.assertEqual(mock_client.post.call_count, 0)

    def test_instagram_publish_single_image_success(self):
        mock_client = MagicMock(spec=httpx.Client)

        # 1. Container creation response
        resp_create = MagicMock(status_code=200, json=lambda: {"id": "c_ig_123"})
        # 2. Status poll response
        resp_poll = MagicMock(status_code=200, json=lambda: {"status_code": "FINISHED"})
        # 3. Publish container response
        resp_publish = MagicMock(status_code=200, json=lambda: {"id": "ig_media_999"})

        mock_client.post.side_effect = [resp_create, resp_publish]
        mock_client.get.return_value = resp_poll

        adapter = InstagramAdapter(client=mock_client)

        with patch("app.adapters.instagram.ensure_fresh_instagram_token", return_value={"access_token": "ig_token", "account_id": "ig_acc_42"}):
            post = {
                "title": "Photo Title",
                "body": "Checking out this sunset! #sunset",
                "media_url": "https://images.unsplash.com/photo-sample.jpg",
            }
            result = adapter.publish(post)
            self.assertTrue(result.success)
            self.assertEqual(result.external_post_id, "ig_media_999")

            # Check container creation parameters
            create_call = mock_client.post.call_args_list[0]
            self.assertEqual(create_call[0][0], "https://graph.facebook.com/v19.0/ig_acc_42/media")
            self.assertEqual(create_call[1]["data"]["image_url"], "https://images.unsplash.com/photo-sample.jpg")
            self.assertEqual(create_call[1]["data"]["caption"], "Photo Title\n\nChecking out this sunset! #sunset")

            # Check publish container call
            pub_call = mock_client.post.call_args_list[1]
            self.assertEqual(pub_call[0][0], "https://graph.facebook.com/v19.0/ig_acc_42/media_publish")
            self.assertEqual(pub_call[1]["data"]["creation_id"], "c_ig_123")

    def test_instagram_publish_video_reel_success(self):
        mock_client = MagicMock(spec=httpx.Client)

        resp_create = MagicMock(status_code=200, json=lambda: {"id": "c_reel_456"})
        resp_poll = MagicMock(status_code=200, json=lambda: {"status_code": "FINISHED"})
        resp_publish = MagicMock(status_code=200, json=lambda: {"id": "ig_reel_777"})

        mock_client.post.side_effect = [resp_create, resp_publish]
        mock_client.get.return_value = resp_poll

        adapter = InstagramAdapter(client=mock_client)

        with patch("app.adapters.instagram.ensure_fresh_instagram_token", return_value={"access_token": "ig_tok", "account_id": "ig_acc_42"}):
            post = {
                "title": None,
                "body": "Reels clip test",
                "media_url": "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4",
            }
            result = adapter.publish(post)
            self.assertTrue(result.success)
            self.assertEqual(result.external_post_id, "ig_reel_777")

            # Verify media_type="REELS" was passed for video
            create_call = mock_client.post.call_args_list[0]
            self.assertEqual(create_call[1]["data"]["media_type"], "REELS")
            self.assertEqual(create_call[1]["data"]["video_url"], "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4")

    def test_instagram_permanent_and_transient_errors(self):
        mock_client = MagicMock(spec=httpx.Client)

        # Permanent error: 400 Bad Request
        resp_400 = MagicMock(status_code=400, json=lambda: {"error": {"message": "Invalid aspect ratio."}})
        mock_client.post.return_value = resp_400

        adapter = InstagramAdapter(client=mock_client)
        with patch("app.adapters.instagram.ensure_fresh_instagram_token", return_value={"access_token": "SECRET_KEY_123", "account_id": "acc_1"}):
            result = adapter.publish({"body": "Hello", "media_url": "https://example.com/bad_ratio.jpg"})
            self.assertFalse(result.success)
            self.assertFalse(result.retryable)
            self.assertNotIn("SECRET_KEY_123", result.error_message)
            self.assertIn("400", result.error_message)

        # Transient error: 429 Rate limit
        resp_429 = MagicMock(status_code=429, json=lambda: {"error": {"message": "User request limit reached."}})
        mock_client.post.return_value = resp_429
        with patch("app.adapters.instagram.ensure_fresh_instagram_token", return_value={"access_token": "tok", "account_id": "acc_1"}):
            result = adapter.publish({"body": "Hello", "media_url": "https://example.com/photo.jpg"})
            self.assertFalse(result.success)
            self.assertTrue(result.retryable)

    def test_dev_endpoints_instagram(self):
        client = TestClient(app)

        with patch("app.adapters.instagram.InstagramAdapter.publish") as mock_pub:
            mock_pub.return_value = PublishResult(success=True, external_post_id="ig_dev_888", retryable=True)
            resp = client.post("/dev/test-instagram", json={"title": "Dev IG", "body": "Dev content", "media_url": "https://example.com/dev.jpg"})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["external_post_id"], "ig_dev_888")

        resp_acc = client.get("/dev/instagram-account")
        self.assertEqual(resp_acc.status_code, 200)
        data = resp_acc.json()
        self.assertIn("status", data)
        # Ensure no token or secret key is present in response
        self.assertNotIn("access_token", data)
        self.assertNotIn("client_secret", data)

    def test_posts_api_accepts_instagram(self):
        client = TestClient(app)

        payload = {
            "title": "Instagram Schedule Test",
            "body": "Cross-posting to Instagram!",
            "media_url": "https://example.com/sunset.jpg",
            "targets": [{"platform": "instagram", "scheduled_at": "2026-10-30T16:00:00"}],
        }
        resp = client.post("/posts", json=payload)
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["targets"][0]["platform"], "instagram")
        self.assertEqual(data["media_url"], "https://example.com/sunset.jpg")


if __name__ == "__main__":
    unittest.main()

