"""
Pattern detection engine for post performance.
Aggregates engagement scores across content features (format, hook style, links, emojis,
timing, language) and computes statistical metrics with strict sample-size safeguards.
"""

from datetime import datetime
import statistics
from typing import List, Dict, Any, Optional
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models.feature import PostFeature
from app.models.target import PostTarget
from app.services.analytics import get_scored_posts
from app.services.tagging import tag_post

MIN_POSTS_OVERALL = 20
MIN_POSTS_PER_GROUP = 5


def get_char_bucket(char_count: int) -> str:
    """Categorizes character count into actionable length ranges."""
    if char_count < 150:
        return "Short (< 150 chars)"
    elif char_count <= 300:
        return "Medium (150-300 chars)"
    elif char_count <= 500:
        return "Detailed (300-500 chars)"
    else:
        return "Longform (500+ chars)"


def get_hour_bucket_ist(hour: int) -> str:
    """Categorizes IST hour into clear daily time windows."""
    if 6 <= hour < 12:
        return "Morning (06:00 - 12:00 IST)"
    elif 12 <= hour < 17:
        return "Afternoon (12:00 - 17:00 IST)"
    elif 17 <= hour < 21:
        return "Evening (17:00 - 21:00 IST)"
    else:
        return "Night / Late (21:00 - 06:00 IST)"


def compute_group_stats(group_posts: List[Dict[str, Any]], total_overall: int) -> Dict[str, Any]:
    """
    Computes sample size, mean score, median score, and vs-average ratio for a group of posts.
    Enforces sample-size safeguards before elevating to a confirmed finding.
    """
    count = len(group_posts)
    if count == 0:
        return {
            "sample_size": 0,
            "avg_score": 0.0,
            "median_score": 0.0,
            "avg_vs_average": None,
            "confidence": "low",
            "is_finding": False,
            "status_text": "No data",
        }

    scores = [p["score"] for p in group_posts]
    avg_score = round(sum(scores) / count, 4)
    median_score = round(statistics.median(scores), 4)

    vs_avgs = [p["vs_average"] for p in group_posts if p.get("vs_average") is not None]
    avg_vs_avg = round(sum(vs_avgs) / len(vs_avgs), 2) if vs_avgs else None

    # Strict sample-size evaluation:
    # A pattern is ONLY a finding if the group has >= 5 posts AND >= 20 scored posts exist overall
    is_valid_finding = (count >= MIN_POSTS_PER_GROUP) and (total_overall >= MIN_POSTS_OVERALL)

    if is_valid_finding:
        confidence = "high" if count >= 10 else "medium"
        status_text = "Verified pattern"
    else:
        confidence = "low"
        status_text = f"Not enough data ({count}/{MIN_POSTS_PER_GROUP} in group, {total_overall}/{MIN_POSTS_OVERALL} overall)"

    return {
        "sample_size": count,
        "avg_score": avg_score,
        "median_score": median_score,
        "avg_vs_average": avg_vs_avg,
        "confidence": confidence,
        "is_finding": is_valid_finding,
        "status_text": status_text,
    }


def analyze_patterns(db: Session, days: int = 90) -> Dict[str, Any]:
    """
    Evaluates patterns across all feature dimensions:
    - format (single, thread, question, list, story, announcement)
    - hook_type (question, number, bold_claim, story_opening, how_to, other)
    - has_link (True, False)
    - has_emoji (True, False)
    - language_guess (hinglish, english, hindi, other)
    - length (char count bucket)
    - timing (hour bucket and weekday in IST)

    Returns tables, summary findings, and sample-size diagnostics.
    """
    scored_posts = get_scored_posts(db, days=days)
    total_scored = len(scored_posts)

    # Attach features to each scored post
    enriched_posts: List[Dict[str, Any]] = []

    for p in scored_posts:
        target_id = p["target_id"]
        stmt = select(PostFeature).where(
            PostFeature.post_target_id == target_id,
            PostFeature.part_index == 0,
        )
        feat = db.scalars(stmt).first()

        # If not tagged yet, run rule-based tagging on the fly
        if not feat:
            target = db.get(PostTarget, target_id)
            if target:
                feats = tag_post(db, target)
                feat = feats[0] if feats else None

        if feat:
            enriched = {
                **p,
                "format": feat.format,
                "hook_type": feat.hook_type,
                "has_link": feat.has_link,
                "has_emoji": feat.has_emoji,
                "emoji_count": feat.emoji_count,
                "hashtag_count": feat.hashtag_count,
                "language_guess": feat.language_guess,
                "char_bucket": get_char_bucket(feat.char_count),
                "hour_bucket": get_hour_bucket_ist(feat.posted_hour_ist),
                "weekday": feat.posted_weekday_ist,
            }
            enriched_posts.append(enriched)

    # Dimensions to break down
    dimensions = {
        "format": ["single", "thread", "question", "list", "story", "announcement"],
        "hook_type": ["question", "number", "bold_claim", "story_opening", "how_to", "other"],
        "has_link": [False, True],
        "has_emoji": [False, True],
        "language_guess": ["english", "hinglish", "hindi", "other"],
        "char_bucket": [
            "Short (< 150 chars)",
            "Medium (150-300 chars)",
            "Detailed (300-500 chars)",
            "Longform (500+ chars)",
        ],
        "hour_bucket": [
            "Morning (06:00 - 12:00 IST)",
            "Afternoon (12:00 - 17:00 IST)",
            "Evening (17:00 - 21:00 IST)",
            "Night / Late (21:00 - 06:00 IST)",
        ],
        "weekday": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    }

    tables: Dict[str, List[Dict[str, Any]]] = {}
    verified_findings: List[Dict[str, Any]] = []

    for dim_name, possible_values in dimensions.items():
        rows: List[Dict[str, Any]] = []
        for val in possible_values:
            matches = [p for p in enriched_posts if p.get(dim_name) == val]
            stats = compute_group_stats(matches, total_overall=total_scored)

            row = {
                "dimension": dim_name,
                "value": str(val) if not isinstance(val, bool) else ("Yes" if val else "No"),
                "raw_value": val,
                **stats,
            }
            rows.append(row)

            # Record verified findings
            if stats["is_finding"] and stats.get("avg_vs_average"):
                ratio = stats["avg_vs_average"]
                if ratio >= 1.2 or ratio <= 0.8:
                    evidence_text = f"Sample: {stats['sample_size']} posts. Avg score {stats['avg_score']} ({ratio}x median)."
                    verified_findings.append({
                        "dimension": dim_name,
                        "value": row["value"],
                        "ratio": ratio,
                        "avg_score": stats["avg_score"],
                        "sample_size": stats["sample_size"],
                        "confidence": stats["confidence"],
                        "evidence": evidence_text,
                    })

        tables[dim_name] = rows

    return {
        "total_scored_posts": total_scored,
        "min_required_overall": MIN_POSTS_OVERALL,
        "min_required_per_group": MIN_POSTS_PER_GROUP,
        "is_data_sufficient": total_scored >= MIN_POSTS_OVERALL,
        "tables": tables,
        "verified_findings": sorted(verified_findings, key=lambda x: x["ratio"], reverse=True),
    }

