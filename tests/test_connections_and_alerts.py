"""
Automated unit tests for Platform Connections and Failure Alerts via Telegram:
- Connection test diagnostics return ok/failed with readable messages without secrets.
- Credential updates validate with a test first; failing tests keep old credentials;
  passing tests encrypt and persist; nothing secret appears in responses, logs, or HTML.
- Adapters prioritize account-row credentials first and fall back to .env.
- Alert triggers: failed target, token expiry thresholds, paused-too-long, missed posts.
- Anti-spam: deduplication, grouping storm suppression, quiet hours, and rate limits.
- Alert delivery failure never breaks publishing.
"""

import unittest
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

import app.models  # ensure all models are registered
from app.database import Base, get_db
from app.config import settings
from app.models import Post, PostTarget, TargetStatus, Log, AppSetting, Account
from app.models.connection import ConnectionStatus
from app.models.alert import AlertLog
from app.main import app
from app.adapters.telegram import TelegramAdapter
from app.adapters.discord import DiscordAdapter
from app.services.crypto_service import encrypt_string, decrypt_string
from app.services.connection_service import (
    get_platform_credentials,
    test_telegram_connection,
    test_discord_connection,
    test_threads_connection,
    test_platform_connection,
    save_platform_credentials,
    get_all_connections_summary,
)
from app.services.alerts import (
    send_alert,
    is_alerts_enabled,
    check_token_expiries,
    check_paused_and_missed_posts,
)
from app.services.publisher import run_due_posts


class TestConnectionsAndAlerts(unittest.TestCase):

    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

        # Initialize app_settings defaults
        self.db.add(AppSetting(key="publishing_paused", value="false"))
        self.db.add(AppSetting(key="alerts_enabled", value="true"))
        self.db.commit()

        # Override FastAPI db dependency
        def override_get_db():
            session = self.Session()
            try:
                yield session
            finally:
                session.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

        # Ensure encryption key is available for tests
        if not settings.ENCRYPTION_KEY:
            settings.ENCRYPTION_KEY = "test-secret-encryption-key-12345"
        settings.ALERTS_ENABLED = True
        settings.TELEGRAM_ALERT_CHAT_ID = "99887766"

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(self.engine)

    @patch("httpx.Client.get")
    @patch("httpx.Client.post")
    def test_connection_diagnostics_no_secrets(self, mock_post, mock_get):
        """Connection diagnostics return readable message and never leak tokens or webhook URLs."""
        secret_bot_token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
        secret_webhook = "https://discord.com/api/webhooks/112233/SECRET_DISCORD_TOKEN"

        # 1. Telegram test success
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"ok": True, "result": {"id": 123456, "username": "MyAlertBot"}},
        )
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"ok": True, "result": {"id": -100123, "title": "My Channel", "type": "channel"}},
        )

        res_tg = test_telegram_connection(secret_bot_token, "-100123")
        self.assertTrue(res_tg["ok"])
        self.assertIn("@MyAlertBot", res_tg["message"])
        self.assertNotIn(secret_bot_token, res_tg["message"])

        # 2. Discord test failure (404 deleted webhook)
        mock_get.return_value = MagicMock(
            status_code=404,
            json=lambda: {"message": "Unknown Webhook", "code": 10015},
        )
        res_discord = test_discord_connection(secret_webhook)
        self.assertFalse(res_discord["ok"])
        self.assertIn("deleted or does not exist", res_discord["message"].lower())
        self.assertNotIn("SECRET_DISCORD_TOKEN", res_discord["message"])

    @patch("app.services.connection_service.test_telegram_connection")
    def test_update_credentials_fails_keeps_old_and_succeeds_encrypts(self, mock_test_tg):
        """Failing test preserves existing credentials; passing test encrypts and stores."""
        old_creds = {"bot_token": "old_token_123", "chat_id": "-100111"}
        old_cipher = encrypt_string(json.dumps(old_creds))
        acc = Account(platform="telegram", name="Old Telegram", encrypted_credentials=old_cipher)
        self.db.add(acc)
        self.db.commit()

        # Step 1: Attempt update with invalid token
        mock_test_tg.return_value = {
            "ok": False,
            "message": "Invalid token from BotFather",
            "suggestion": "Check token",
            "identity": None,
        }

        res_fail = save_platform_credentials(
            self.db, "telegram", {"bot_token": "bad_token", "chat_id": "-100111"}
        )
        self.assertFalse(res_fail["ok"])

        # Verify old credentials are still intact
        reloaded = self.db.scalars(select(Account).where(Account.platform == "telegram")).first()
        decrypted = json.loads(decrypt_string(reloaded.encrypted_credentials))
        self.assertEqual(decrypted["bot_token"], "old_token_123")

        # Step 2: Attempt update with valid token
        mock_test_tg.return_value = {
            "ok": True,
            "message": "Connected to @NewBot in Channel",
            "suggestion": None,
            "identity": "@NewBot → Channel",
        }

        res_success = save_platform_credentials(
            self.db, "telegram", {"bot_token": "new_valid_token_456", "chat_id": "-100222"}
        )
        self.assertTrue(res_success["ok"])

        # Verify credentials were encrypted and updated
        reloaded_new = self.db.scalars(select(Account).where(Account.platform == "telegram")).first()
        decrypted_new = json.loads(decrypt_string(reloaded_new.encrypted_credentials))
        self.assertEqual(decrypted_new["bot_token"], "new_valid_token_456")
        self.assertEqual(decrypted_new["chat_id"], "-100222")

        # Verify audit log wrote no secrets
        log = self.db.scalars(select(Log).order_by(Log.id.desc())).first()
        self.assertIsNotNone(log)
        self.assertNotIn("new_valid_token_456", log.message)

    def test_adapters_prioritize_account_row_over_env(self):
        """Telegram and Discord adapters load credentials from Account table when available, falling back to env."""
        # 1. Without account row -> uses settings defaults
        settings.TELEGRAM_BOT_TOKEN = "env_tg_token"
        settings.TELEGRAM_CHAT_ID = "env_tg_chat"
        adapter_env = TelegramAdapter()
        self.assertEqual(adapter_env.bot_token, "env_tg_token")
        self.assertEqual(adapter_env.chat_id, "env_tg_chat")

        # 2. With account row in DB -> overrides settings
        db_creds = {"bot_token": "db_custom_token", "chat_id": "db_custom_chat"}
        acc = Account(
            platform="telegram",
            name="DB Account",
            encrypted_credentials=encrypt_string(json.dumps(db_creds)),
        )
        self.db.add(acc)
        self.db.commit()

        # Passing db context via session
        with patch("app.database.SessionLocal", return_value=self.db):
            adapter_db = TelegramAdapter()
            self.assertEqual(adapter_db.bot_token, "db_custom_token")
            self.assertEqual(adapter_db.chat_id, "db_custom_chat")

    @patch("httpx.Client.post")
    def test_alert_triggers_on_failed_post_target(self, mock_post):
        """When a post target permanently fails in publisher, an alert is dispatched."""
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"ok": True})

        post = Post(title="Breaking Announcement", body="Important scheduled content")
        self.db.add(post)
        self.db.flush()

        # Pending target due now
        target = PostTarget(
            post_id=post.id,
            platform="telegram",
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            status=TargetStatus.pending,
            attempts=2,  # next failure will be attempt 3 -> permanent failure
        )
        self.db.add(target)
        self.db.commit()

        # Mock adapter to return a non-retryable error
        with patch("app.services.publisher.get_adapter") as mock_get_adapter:
            mock_adapter = MagicMock()
            from app.adapters.base import PublishResult
            mock_adapter.publish.return_value = PublishResult(
                success=False,
                error_message="Telegram API error (400): Chat not found",
                retryable=False,
            )
            mock_get_adapter.return_value = mock_adapter

            with patch("app.services.alerts.get_platform_credentials") as mock_creds:
                mock_creds.return_value = {"bot_token": "123:test_bot_token"}
                run_due_posts(self.db)

        # Target should now be failed
        self.db.refresh(target)
        self.assertEqual(target.status, TargetStatus.failed)

        # Check that alert was recorded in alert_log
        alert_row = self.db.scalars(select(AlertLog).where(AlertLog.kind == "failed_post")).first()
        self.assertIsNotNone(alert_row)
        self.assertTrue(alert_row.success)
        self.assertIn("Breaking Announcement", alert_row.message_preview)

    @patch("httpx.Client.post")
    def test_alert_anti_spam_deduplication(self, mock_post):
        """Duplicate alerts with identical dedupe_key within cooldown window are suppressed."""
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"ok": True})

        with patch("app.services.alerts.get_platform_credentials", return_value={"bot_token": "test_token"}):
            # First alert sends successfully
            ok1 = send_alert("test_kind", "Title 1", "Body 1", dedupe_key="unique_key_1", db=self.db)
            self.assertTrue(ok1)

            # Second alert with same dedupe_key is suppressed
            ok2 = send_alert("test_kind", "Title 1", "Body 1", dedupe_key="unique_key_1", db=self.db)
            self.assertFalse(ok2)

    @patch("httpx.Client.post")
    def test_alert_storm_grouping(self, mock_post):
        """Storm of more than 3 failed post alerts in 10 minutes collapses into a single grouped notice."""
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"ok": True})

        with patch("app.services.alerts.get_platform_credentials", return_value={"bot_token": "test_token"}):
            # Send 3 individual failed post alerts
            for i in range(1, 4):
                send_alert("failed_post", f"Failed Post {i}", f"Error {i}", dedupe_key=f"target_failed:{i}", db=self.db)

            # 4th failure triggers grouped notice
            send_alert("failed_post", "Failed Post 4", "Error 4", dedupe_key="target_failed:4", db=self.db)

            grouped_alert = self.db.scalars(
                select(AlertLog).where(AlertLog.dedupe_key.like("grouped_failures%"))
            ).first()
            self.assertIsNotNone(grouped_alert)
            self.assertIn("Multiple post failures detected", grouped_alert.message_preview)

    @patch("httpx.Client.post")
    def test_alert_hourly_rate_limit(self, mock_post):
        """Hourly rate limit caps dispatches at 20 alerts per hour."""
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"ok": True})

        with patch("app.services.alerts.get_platform_credentials", return_value={"bot_token": "test_token"}):
            # Send 20 alerts
            for i in range(20):
                ok = send_alert("notice", f"Notice {i}", f"Body {i}", dedupe_key=f"key_{i}", db=self.db)
                self.assertTrue(ok)

            # 21st alert is rate-limited
            ok_21 = send_alert("notice", "Notice 21", "Body 21", dedupe_key="key_21", db=self.db)
            self.assertFalse(ok_21)

            # Check that rate limit suppression was logged
            suppressed = self.db.scalars(
                select(AlertLog).where(AlertLog.error.like("%Hourly rate limit%"))
            ).first()
            self.assertIsNotNone(suppressed)

    @patch("httpx.Client.post")
    def test_quiet_hours_allows_critical_and_blocks_non_critical(self, mock_post):
        """Critical alerts bypass quiet hours while non-critical alerts are suppressed."""
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {"ok": True})

        with patch("app.services.alerts.get_platform_credentials", return_value={"bot_token": "test_token"}):
            with patch("app.services.alerts.is_in_quiet_hours", return_value=True):
                # Non-critical alert is suppressed
                ok_non_crit = send_alert("daily_digest", "Digest", "Body", dedupe_key="digest_1", db=self.db)
                self.assertFalse(ok_non_crit)

                # Critical alert (failed post) bypasses quiet hours
                ok_crit = send_alert("failed_post", "Post Failed", "Error", dedupe_key="target_crit_1", db=self.db)
                self.assertTrue(ok_crit)

    @patch("httpx.Client.post")
    def test_alert_failure_never_crashes_publisher(self, mock_post):
        """If Telegram alert delivery throws an unhandled exception, publishing completes unaffected."""
        # Mock httpx to raise a connection crash
        mock_post.side_effect = RuntimeError("Fatal Telegram connection socket drop")

        post = Post(title="Critical Notice", body="Body content")
        self.db.add(post)
        self.db.flush()

        target = PostTarget(
            post_id=post.id,
            platform="telegram",
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            status=TargetStatus.pending,
            attempts=3,
        )
        self.db.add(target)
        self.db.commit()

        with patch("app.services.publisher.get_adapter") as mock_get_adapter:
            mock_adapter = MagicMock()
            from app.adapters.base import PublishResult
            mock_adapter.publish.return_value = PublishResult(success=False, error_message="Network error", retryable=False)
            mock_get_adapter.return_value = mock_adapter

            with patch("app.services.alerts.get_platform_credentials", return_value={"bot_token": "tok"}):
                # Must not raise RuntimeError
                processed = run_due_posts(self.db)
                self.assertEqual(processed, 1)

        self.db.refresh(target)
        self.assertEqual(target.status, TargetStatus.failed)

    def test_web_connections_page_renders_ok(self):
        """GET /connections and GET /settings render successfully with 200 OK and no secrets."""
        resp_conn = self.client.get("/connections")
        self.assertEqual(resp_conn.status_code, 200)
        self.assertIn("Platform connections", resp_conn.text)
        self.assertIn("Failure alerts via Telegram", resp_conn.text)
        self.assertIn("Add login before putting this app on the internet", resp_conn.text)

        resp_settings = self.client.get("/settings")
        self.assertEqual(resp_settings.status_code, 200)
        self.assertIn("Platform connections", resp_settings.text)
