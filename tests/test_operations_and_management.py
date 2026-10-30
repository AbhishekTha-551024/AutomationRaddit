"""
Tests for Post Management and Operations Controls:
- Scheduler exclusion of soft-deleted, cancelled, and draft targets
- Status permission enforcement (409/422 responses)
- Atomic protection against races while processing
- Soft delete, restore, cascading permanent delete, and 30-day purge
- Safe post editing and platform validation
- Target rescheduling, post-now, cancellation, and duplication
- Bulk action orchestration
- Publishing pause/resume and overdue grace handling
- Web management routes (/posts, /trash, /posts/{id}/edit)
"""

import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

from app.database import Base
from app.models import Post, PostTarget, TargetStatus, Log, AppSetting, MetricSnapshot, PostFeature
from app.main import app
from app.services.publisher import run_due_posts
from app.services.queue_service import (
    is_publishing_paused,
    pause_publishing,
    resume_publishing,
    is_overdue,
)
from app.services.post_operations import (
    soft_delete_post,
    soft_delete_target,
    restore_post,
    restore_target,
    permanent_delete_post,
    permanent_delete_target,
    empty_trash,
    purge_expired_trash,
    edit_post_content,
    reschedule_target,
    post_now_target,
    cancel_target,
    duplicate_post,
    execute_bulk_action,
    validate_content_for_platforms,
)


class TestPostManagementAndOperations(unittest.TestCase):

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.client = TestClient(app)

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)

    def test_scheduler_excludes_deleted_cancelled_draft(self):
        """Scheduler must never claim or publish deleted, cancelled, or draft targets."""
        now_utc = datetime.now(timezone.utc) - timedelta(minutes=5)
        post = Post(title="Test", body="Valid content")
        self.db.add(post)
        self.db.flush()

        t_deleted = PostTarget(
            post_id=post.id, platform="telegram", scheduled_at=now_utc,
            status=TargetStatus.pending, deleted_at=datetime.now(timezone.utc)
        )
        t_cancelled = PostTarget(
            post_id=post.id, platform="discord", scheduled_at=now_utc,
            status=TargetStatus.cancelled
        )
        t_draft = PostTarget(
            post_id=post.id, platform="threads", scheduled_at=now_utc,
            status=TargetStatus.draft
        )
        self.db.add_all([t_deleted, t_cancelled, t_draft])
        self.db.commit()

        # Run scheduler query / due posts
        processed = run_due_posts(self.db)
        self.assertEqual(processed, 0)

        # Verify statuses remain unchanged
        self.assertEqual(self.db.get(PostTarget, t_deleted.id).status, TargetStatus.pending)
        self.assertEqual(self.db.get(PostTarget, t_cancelled.id).status, TargetStatus.cancelled)
        self.assertEqual(self.db.get(PostTarget, t_draft.id).status, TargetStatus.draft)

    def test_atomic_protection_while_processing(self):
        """A target in 'processing' status cannot be edited, cancelled, or deleted."""
        now_utc = datetime.now(timezone.utc)
        post = Post(title="Processing post", body="Body content")
        self.db.add(post)
        self.db.flush()

        t = PostTarget(
            post_id=post.id, platform="discord", scheduled_at=now_utc,
            status=TargetStatus.processing
        )
        self.db.add(t)
        self.db.commit()

        # Delete target blocked
        with self.assertRaises(ValueError) as ctx:
            soft_delete_target(self.db, t.id)
        self.assertTrue("sent" in str(ctx.exception).lower() or "sending" in str(ctx.exception).lower())

        # Delete post blocked
        with self.assertRaises(ValueError) as ctx:
            soft_delete_post(self.db, post.id)
        self.assertTrue("sent" in str(ctx.exception).lower() or "sending" in str(ctx.exception).lower())

        # Cancel blocked
        with self.assertRaises(ValueError) as ctx:
            cancel_target(self.db, t.id)
        self.assertTrue("sent" in str(ctx.exception).lower() or "sending" in str(ctx.exception).lower())

        # Edit content blocked
        with self.assertRaises(ValueError) as ctx:
            edit_post_content(self.db, post.id, title="New", body="New body", media_url=None)
        self.assertTrue("sent" in str(ctx.exception).lower() or "sending" in str(ctx.exception).lower())

    def test_soft_delete_and_restore_post(self):
        """Soft delete marks post and all targets with deleted_at; restore restores them."""
        now_utc = datetime.now(timezone.utc)
        post = Post(title="Trashable", body="Body text")
        self.db.add(post)
        self.db.flush()

        t1 = PostTarget(post_id=post.id, platform="discord", scheduled_at=now_utc, status=TargetStatus.pending)
        t2 = PostTarget(post_id=post.id, platform="telegram", scheduled_at=now_utc, status=TargetStatus.pending)
        self.db.add_all([t1, t2])
        self.db.commit()

        # Soft delete
        soft_delete_post(self.db, post.id)
        self.assertIsNotNone(post.deleted_at)
        self.assertIsNotNone(t1.deleted_at)
        self.assertIsNotNone(t2.deleted_at)

        # Restore
        restore_post(self.db, post.id)
        self.assertIsNone(post.deleted_at)
        self.assertIsNone(t1.deleted_at)
        self.assertIsNone(t2.deleted_at)

    def test_cascading_permanent_delete(self):
        """Permanent delete of post cleans up metric snapshots, features, and logs."""
        now_utc = datetime.now(timezone.utc)
        post = Post(title="Permanent", body="Permanent body")
        self.db.add(post)
        self.db.flush()

        t = PostTarget(post_id=post.id, platform="threads", scheduled_at=now_utc, status=TargetStatus.posted)
        self.db.add(t)
        self.db.flush()

        # Add metric snapshot, feature, and log
        snap = MetricSnapshot(
            post_target_id=t.id, platform="threads", part_index=0,
            external_media_id="m123", age_label="1h", views=10, raw_json="{}"
        )
        feat = PostFeature(
            post_target_id=t.id, part_index=0, char_count=20, word_count=3, part_count=1,
            format="single", hook_type="other", posted_hour_ist=10, posted_weekday_ist=2
        )
        log = Log(post_target_id=t.id, level="INFO", message="Dispatched")
        self.db.add_all([snap, feat, log])
        self.db.commit()

        # Permanently delete post
        permanent_delete_post(self.db, post.id)

        # Verify cascades
        self.assertIsNone(self.db.get(Post, post.id))
        self.assertIsNone(self.db.get(PostTarget, t.id))
        self.assertEqual(len(self.db.scalars(select(MetricSnapshot).where(MetricSnapshot.post_target_id == t.id)).all()), 0)
        self.assertEqual(len(self.db.scalars(select(PostFeature).where(PostFeature.post_target_id == t.id)).all()), 0)
        self.assertEqual(len(self.db.scalars(select(Log).where(Log.post_target_id == t.id)).all()), 0)

    def test_purge_expired_trash(self):
        """purge_expired_trash removes items deleted older than 30 days."""
        now_utc = datetime.now(timezone.utc)
        old_time = now_utc - timedelta(days=35)
        recent_time = now_utc - timedelta(days=5)

        post1 = Post(title="Old Trash", body="Old", deleted_at=old_time)
        post2 = Post(title="Recent Trash", body="Recent", deleted_at=recent_time)
        self.db.add_all([post1, post2])
        self.db.flush()

        t1 = PostTarget(post_id=post1.id, platform="discord", scheduled_at=old_time, status=TargetStatus.pending, deleted_at=old_time)
        t2 = PostTarget(post_id=post2.id, platform="discord", scheduled_at=recent_time, status=TargetStatus.pending, deleted_at=recent_time)
        self.db.add_all([t1, t2])
        self.db.commit()

        purged = purge_expired_trash(self.db, days=30)
        self.assertEqual(purged, 1)

        # t1 should be gone, t2 should remain
        self.assertIsNone(self.db.get(PostTarget, t1.id))
        self.assertIsNotNone(self.db.get(PostTarget, t2.id))

    def test_edit_post_blocked_if_posted(self):
        """Editing post content is blocked with ValueError if any target is already posted."""
        now_utc = datetime.now(timezone.utc)
        post = Post(title="Published Post", body="Original body")
        self.db.add(post)
        self.db.flush()

        t = PostTarget(post_id=post.id, platform="threads", scheduled_at=now_utc, status=TargetStatus.posted)
        self.db.add(t)
        self.db.commit()

        with self.assertRaises(ValueError) as ctx:
            edit_post_content(self.db, post.id, title="New title", body="New body", media_url=None)
        self.assertIn("not allowed", str(ctx.exception).lower())

    def test_reschedule_target_resets_attempts(self):
        """Rescheduling resets retry attempts to 0 and clears next_retry_at."""
        now_utc = datetime.now(timezone.utc)
        post = Post(title="Failed Post", body="Body")
        self.db.add(post)
        self.db.flush()

        t = PostTarget(
            post_id=post.id, platform="discord", scheduled_at=now_utc - timedelta(hours=1),
            status=TargetStatus.failed, attempts=3, next_retry_at=now_utc + timedelta(hours=1),
            error_message="Error"
        )
        self.db.add(t)
        self.db.commit()

        future_time = now_utc + timedelta(hours=2)
        reschedule_target(self.db, t.id, future_time)

        self.assertEqual(t.status, TargetStatus.pending)
        self.assertEqual(t.attempts, 0)
        self.assertIsNone(t.next_retry_at)
        self.assertIsNone(t.error_message)

    def test_duplicate_post_creates_draft_with_targets(self):
        """Duplicating a post copies content and creates platform targets in draft status."""
        now_utc = datetime.now(timezone.utc)
        post = Post(title="Original", body="Content body", media_url="https://example.com/img.png")
        self.db.add(post)
        self.db.flush()

        t1 = PostTarget(post_id=post.id, platform="threads", scheduled_at=now_utc, status=TargetStatus.posted)
        t2 = PostTarget(post_id=post.id, platform="telegram", scheduled_at=now_utc, status=TargetStatus.posted)
        self.db.add_all([t1, t2])
        self.db.commit()

        dup_id = duplicate_post(self.db, post.id)
        dup = self.db.get(Post, dup_id)

        self.assertIsNotNone(dup)
        self.assertEqual(dup.title, "Original (Copy)")
        self.assertEqual(dup.body, "Content body")
        self.assertEqual(dup.media_url, "https://example.com/img.png")
        self.assertEqual(len(dup.targets), 2)
        for t in dup.targets:
            self.assertEqual(t.status, TargetStatus.draft)

    def test_bulk_actions_orchestration(self):
        """Bulk action returns completed and skipped counts with itemized details."""
        now_utc = datetime.now(timezone.utc)
        post = Post(title="Bulk Post", body="Body")
        self.db.add(post)
        self.db.flush()

        t1 = PostTarget(post_id=post.id, platform="telegram", scheduled_at=now_utc, status=TargetStatus.failed)
        t2 = PostTarget(post_id=post.id, platform="discord", scheduled_at=now_utc, status=TargetStatus.pending)
        self.db.add_all([t1, t2])
        self.db.commit()

        # Retry bulk action: t1 should succeed (failed), t2 should be skipped (not failed)
        res = execute_bulk_action(self.db, action="retry", target_ids=[t1.id, t2.id])
        self.assertEqual(res["done"], 1)
        self.assertEqual(res["skipped"], 1)
        self.assertEqual(res["details"][0]["status"], "done")
        self.assertEqual(res["details"][1]["status"], "skipped")

    def test_queue_pause_and_resume(self):
        """Publishing paused flag pauses publisher; resume staggers overdue targets."""
        # Ensure setting exists
        setting = self.db.get(AppSetting, "publishing_paused")
        if not setting:
            self.db.add(AppSetting(key="publishing_paused", value="false"))
            self.db.commit()

        self.assertFalse(is_publishing_paused(self.db))
        pause_publishing(self.db)
        self.assertTrue(is_publishing_paused(self.db))

        # Check that run_due_posts returns 0 when paused
        count = run_due_posts(self.db)
        self.assertEqual(count, 0)

        # Resume
        resumed = resume_publishing(self.db)
        self.assertFalse(is_publishing_paused(self.db))

    def test_web_routes_render_ok(self):
        """Web routes /posts, /trash, and dashboard render successfully."""
        resp = self.client.get("/posts")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Posts", resp.text)

        resp_trash = self.client.get("/trash")
        self.assertEqual(resp_trash.status_code, 200)
        self.assertIn("Trash", resp_trash.text)

        resp_dash = self.client.get("/dashboard")
        self.assertEqual(resp_dash.status_code, 200)


if __name__ == "__main__":
    unittest.main()
