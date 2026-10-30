"""
Unit tests for:
1. Rule-based tagging (hook detection, language guess, emoji count)
2. Pattern engine sample-size safeguards (no findings below minimum sample)
3. Playbook rule management and markdown export
4. AI Analyst budget guard and mocked completion
5. Analytics and Playbook API endpoints
"""

import unittest
from datetime import datetime, timezone, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient
from app.database import Base
from app.config import settings
from app.models.post import Post
from app.models.target import PostTarget, TargetStatus
from app.models.feature import PostFeature
from app.models.metric import MetricSnapshot, AccountSnapshot
from app.models.playbook import PlaybookRule
from app.models.analyst import UsageLog, AnalysisReport
from app.services.tagging import (
    detect_hook_type,
    detect_format,
    detect_language,
    extract_features_for_part,
    tag_post,
)
from app.services.patterns import analyze_patterns, compute_group_stats
from app.services.playbook_service import (
    create_rule,
    list_rules,
    update_rule_status,
    delete_rule,
    get_active_playbook_text,
)
from app.services.analyst import (
    check_analyst_readiness,
    calculate_cost,
    clean_json_response,
    run_ai_analyst,
)
from app.main import app


class MockXAIClient:
    """Mock client simulating xAI Grok responses without making network requests."""
    def __init__(self, response_text: str = None, input_tokens: int = 400, output_tokens: int = 150):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.response_text = response_text or (
            '{\n'
            '  "summary": "Posts featuring numbered hooks significantly outperformed general commentary.",\n'
            '  "observed_patterns": [\n'
            '    {\n'
            '      "rule_text": "Lead technical posts with a specific number of items.",\n'
            '      "category": "hook",\n'
            '      "confidence": "high",\n'
            '      "evidence": "12 posts with numbers averaged 2.4x median score."\n'
            '    }\n'
            '  ],\n'
            '  "experiment_ideas": [\n'
            '    "Compare single post vs 3-part thread on architecture topics.",\n'
            '    "Test evening IST vs morning IST timing.",\n'
            '    "Test question hook vs numbered hook on Python tips."\n'
            '  ]\n'
            '}'
        )

    def create_completion(self, model: str, messages: list):
        return {
            "content": self.response_text,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


class TestTaggingHeuristics(unittest.TestCase):
    """Tests rule-based tagging functions."""

    def test_hook_detection(self):
        # Question hooks
        self.assertEqual(detect_hook_type("Why do 90% of developers quit?"), "question")
        self.assertEqual(detect_hook_type("What is the biggest trap in coding?"), "question")
        self.assertEqual(detect_hook_type("Kya aapko pata hai?"), "question")

        # Number hooks
        self.assertEqual(detect_hook_type("5 rules for writing cleaner code:"), "number")
        self.assertEqual(detect_hook_type("3 mistakes that cost me months:"), "number")

        # How-to hooks
        self.assertEqual(detect_hook_type("How to scale PostgreSQL in 2026"), "how_to")
        self.assertEqual(detect_hook_type("How I built a post scheduler from scratch"), "how_to")

        # Bold claims
        self.assertEqual(detect_hook_type("The truth about ORMs nobody wants to admit"), "bold_claim")
        self.assertEqual(detect_hook_type("Never use print statements in production"), "bold_claim")

        # Story openings
        self.assertEqual(detect_hook_type("Last year I quit my job with 0 followers"), "story_opening")
        self.assertEqual(detect_hook_type("When I started programming in 2020"), "story_opening")

        # Other fallback
        self.assertEqual(detect_hook_type("Just another sunny day in Bangalore."), "other")

    def test_format_detection(self):
        self.assertEqual(detect_format("Single body text", part_count=3), "thread")
        self.assertEqual(detect_format("Here are tips:\n- Tip 1\n- Tip 2\n- Tip 3", part_count=1), "list")
        self.assertEqual(detect_format("Excited to announce our new open source tool!", part_count=1), "announcement")
        self.assertEqual(detect_format("Which framework do you prefer for APIs?", part_count=1), "question")
        self.assertEqual(detect_format("Simple plain paragraph.", part_count=1), "single")

    def test_language_detection(self):
        self.assertEqual(detect_language("यह एक हिंदी में लिखा गया पोस्ट है"), "hindi")
        self.assertEqual(detect_language("Yeh post bohot accha hai bhai, kya baat hai yaar"), "hinglish")
        self.assertEqual(detect_language("Building a production grade scheduler using FastAPI and SQLite."), "english")

    def test_feature_extraction(self):
        text = "5 tips for better sleep 🌙💤:\n- No screens\n- Cool room\nhttps://example.com #health"
        feat = extract_features_for_part(
            part_text=text,
            part_index=0,
            part_count=1,
            posted_at_utc=datetime(2026, 10, 30, 4, 30, tzinfo=timezone.utc),  # 10:00 AM IST
        )
        self.assertEqual(feat["hook_type"], "number")
        self.assertEqual(feat["format"], "list")
        self.assertTrue(feat["has_link"])
        self.assertTrue(feat["has_emoji"])
        self.assertEqual(feat["emoji_count"], 2)
        self.assertEqual(feat["hashtag_count"], 1)
        self.assertEqual(feat["posted_hour_ist"], 10)
        self.assertEqual(feat["posted_weekday_ist"], "Friday")


class TestPatternEngineSafeguards(unittest.TestCase):
    """Tests that patterns are never reported as findings without adequate sample sizes."""

    def test_small_sample_rejected_as_finding(self):
        # 3 posts in group, 10 overall -> MUST NOT be a finding
        small_group = [
            {"score": 2.5, "vs_average": 2.0},
            {"score": 3.0, "vs_average": 2.2},
            {"score": 2.0, "vs_average": 1.8},
        ]
        stats = compute_group_stats(small_group, total_overall=10)
        self.assertFalse(stats["is_finding"])
        self.assertEqual(stats["confidence"], "low")
        self.assertIn("Not enough data", stats["status_text"])

    def test_sufficient_sample_confirmed_as_finding(self):
        # 6 posts in group, 25 overall -> qualifies as finding
        group = [{"score": 2.0, "vs_average": 1.5} for _ in range(6)]
        stats = compute_group_stats(group, total_overall=25)
        self.assertTrue(stats["is_finding"])
        self.assertIn(stats["confidence"], ["medium", "high"])
        self.assertEqual(stats["status_text"], "Verified pattern")


class TestPlaybookService(unittest.TestCase):
    """Tests Playbook rule approval workflow and markdown prompt generation."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()

    def test_rule_crud_and_status_transitions(self):
        # 1. Create suggested rule
        rule = create_rule(
            self.db,
            rule_text="Keep captions under 250 characters",
            category="length",
            source="stats",
            confidence="medium",
            evidence="Sample: 14 posts",
            status="suggested",
        )
        self.assertEqual(rule.status, "suggested")

        # 2. User activates rule
        activated = update_rule_status(self.db, rule.id, "active")
        self.assertEqual(activated.status, "active")

        # 3. Export includes active rule
        export_text = get_active_playbook_text(self.db)
        self.assertIn("Keep captions under 250 characters", export_text)
        self.assertIn("Length", export_text)

        # 4. User rejects rule
        rejected = update_rule_status(self.db, rule.id, "rejected")
        self.assertEqual(rejected.status, "rejected")

        # 5. Export does not include rejected rule
        export_text_after = get_active_playbook_text(self.db)
        self.assertNotIn("Keep captions under 250 characters", export_text_after)

        # 6. Delete rule
        deleted = delete_rule(self.db, rule.id)
        self.assertTrue(deleted)
        self.assertEqual(len(list_rules(self.db)), 0)


class TestAIAnalystBudgetGuard(unittest.TestCase):
    """Tests that the AI Analyst strictly honors configuration, minimum posts, and monthly budgets."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()

    def test_cost_calculation(self):
        # 1,000,000 input tokens at $2 = $2.00
        # 1,000,000 output tokens at $10 = $10.00
        cost = calculate_cost(1000, 500)
        # (1000*2 + 500*10)/1M = (2000 + 5000)/1M = 7000/1M = 0.007
        self.assertEqual(cost, 0.007)

    def test_unconfigured_stops_gracefully(self):
        old_key = settings.XAI_API_KEY
        settings.XAI_API_KEY = None
        try:
            readiness = check_analyst_readiness(self.db)
            self.assertFalse(readiness["ready"])
            self.assertEqual(readiness["status"], "not_configured")
        finally:
            settings.XAI_API_KEY = old_key

    def test_insufficient_posts_waits_for_data(self):
        old_key = settings.XAI_API_KEY
        old_model = settings.XAI_MODEL
        settings.XAI_API_KEY = "test-key"
        settings.XAI_MODEL = "grok-2"
        try:
            readiness = check_analyst_readiness(self.db)
            self.assertFalse(readiness["ready"])
            self.assertEqual(readiness["status"], "waiting_data")
            self.assertIn("Waiting for more data", readiness["message"])
        finally:
            settings.XAI_API_KEY = old_key
            settings.XAI_MODEL = old_model

    def test_budget_exceeded_guard(self):
        old_key = settings.XAI_API_KEY
        old_model = settings.XAI_MODEL
        old_budget = settings.AI_MONTHLY_BUDGET_USD
        settings.XAI_API_KEY = "test-key"
        settings.XAI_MODEL = "grok-2"
        settings.AI_MONTHLY_BUDGET_USD = 1.0

        try:
            # Simulate prior spending of $1.50 this month
            self.db.add(UsageLog(
                feature="analyst",
                model="grok-2",
                input_tokens=500_000,
                output_tokens=50_000,
                estimated_cost_usd=1.50,
                created_at=datetime.now(timezone.utc),
            ))
            self.db.commit()

            readiness = check_analyst_readiness(self.db)
            self.assertFalse(readiness["ready"])
            self.assertEqual(readiness["status"], "budget_exceeded")
            self.assertIn("Monthly budget limit reached", readiness["message"])
        finally:
            settings.XAI_API_KEY = old_key
            settings.XAI_MODEL = old_model
            settings.AI_MONTHLY_BUDGET_USD = old_budget

    def test_mock_analyst_run_generates_suggested_rules(self):
        old_key = settings.XAI_API_KEY
        old_model = settings.XAI_MODEL
        old_min = settings.ANALYST_MIN_POSTS
        settings.XAI_API_KEY = "mock-key"
        settings.XAI_MODEL = "grok-2"
        settings.ANALYST_MIN_POSTS = 1

        try:
            # Seed 1 post with a snapshot so readiness passes
            post = Post(title="Test Post", body="5 rules for coding")
            self.db.add(post)
            self.db.commit()

            target = PostTarget(
                post_id=post.id,
                platform="threads",
                status=TargetStatus.posted,
                posted_at=datetime.now(timezone.utc) - timedelta(hours=2),
                scheduled_at=datetime.now(timezone.utc) - timedelta(hours=2),
                external_post_id="mock_123",
            )
            self.db.add(target)
            self.db.commit()

            snap = MetricSnapshot(
                post_target_id=target.id,
                platform="threads",
                part_index=0,
                external_media_id="mock_123",
                age_label="1h",
                views=100,
                likes=15,
                replies=4,
                reposts=2,
                quotes=1,
                shares=3,
                raw_json="{}",
            )
            self.db.add(snap)
            self.db.commit()

            client = MockXAIClient()
            result = run_ai_analyst(self.db, days=30, client=client)

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["suggested_rules_created"], 1)

            # Confirm rule was created ONLY as 'suggested' (never active)
            rules = self.db.query(PlaybookRule).all()
            self.assertEqual(len(rules), 1)
            self.assertEqual(rules[0].status, "suggested")
            self.assertEqual(rules[0].source, "ai")
            self.assertIn("Lead technical posts with a specific number", rules[0].rule_text)

            # Confirm usage was logged
            usages = self.db.query(UsageLog).all()
            self.assertEqual(len(usages), 1)
            self.assertGreater(usages[0].estimated_cost_usd, 0.0)

            # Confirm analysis report was saved
            reports = self.db.query(AnalysisReport).all()
            self.assertEqual(len(reports), 1)
            self.assertIn("Posts featuring numbered hooks", reports[0].summary_text)

        finally:
            settings.XAI_API_KEY = old_key
            settings.XAI_MODEL = old_model
            settings.ANALYST_MIN_POSTS = old_min


class TestEndpoints(unittest.TestCase):
    """Tests API routes for Analytics, Playbook, and Dev utilities."""

    def setUp(self):
        self.client = TestClient(app)

    def test_analytics_endpoints_ok(self):
        resp_posts = self.client.get("/analytics/posts")
        self.assertEqual(resp_posts.status_code, 200)
        self.assertIsInstance(resp_posts.json(), list)

        resp_summary = self.client.get("/analytics/summary")
        self.assertEqual(resp_summary.status_code, 200)
        self.assertIn("total_views", resp_summary.json())

        resp_patterns = self.client.get("/analytics/patterns")
        self.assertEqual(resp_patterns.status_code, 200)
        self.assertIn("tables", resp_patterns.json())

    def test_playbook_endpoints_ok(self):
        # 1. Create rule
        resp_create = self.client.post("/playbook/rules", json={
            "rule_text": "Always include a question at the end",
            "category": "hook",
            "confidence": "high",
            "evidence": "Observed in 20 posts",
            "status": "active",
        })
        self.assertEqual(resp_create.status_code, 200)
        rule_id = resp_create.json()["rule"]["id"]

        # 2. List rules
        resp_list = self.client.get("/playbook/rules?status=active")
        self.assertEqual(resp_list.status_code, 200)
        self.assertTrue(any(r["id"] == rule_id for r in resp_list.json()))

        # 3. Export
        resp_export = self.client.get("/playbook/export")
        self.assertEqual(resp_export.status_code, 200)
        self.assertIn("Always include a question at the end", resp_export.text)

        # 4. Patch status
        resp_patch = self.client.patch(f"/playbook/rules/{rule_id}", json={"status": "rejected"})
        self.assertEqual(resp_patch.status_code, 200)

        # 5. Delete
        resp_del = self.client.delete(f"/playbook/rules/{rule_id}")
        self.assertEqual(resp_del.status_code, 200)

    def test_dev_backfill_tags(self):
        resp = self.client.post("/dev/backfill-tags")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("tagged_targets", resp.json())


if __name__ == "__main__":
    unittest.main()

