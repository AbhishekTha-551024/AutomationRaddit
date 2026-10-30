"""
Rule-based post feature tagging service.
Extracts structural, lexical, formatting, and timing features without AI costs.
"""

from datetime import datetime, timezone
import logging
import re
from typing import List, Tuple, Optional
from zoneinfo import ZoneInfo
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models.post import Post
from app.models.target import PostTarget
from app.models.feature import PostFeature
from app.adapters.threads import split_thread_parts

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Regex for detecting URLs
URL_PATTERN = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)

# Regex for detecting hashtags
HASHTAG_PATTERN = re.compile(r"#\w+", re.UNICODE)

# Regex for detecting emojis across standard Unicode ranges
EMOJI_PATTERN = re.compile(
    r"[\U00010000-\U0010ffff"  # Supplemental symbols, pictographs, emojis
    r"\u2600-\u27bf"           # Miscellaneous symbols & dingbats
    r"\u2300-\u23ff"           # Miscellaneous technical
    r"\u2b50\u2b55\u2934\u2935" # Specific arrows and stars
    r"]"
)

# Common Romanized Hindi words for Hinglish detection
HINGLISH_WORDS = {
    "kya", "hai", "nahi", "hota", "hote", "kare", "karna", "hum", "yeh", "woh",
    "bhi", "mein", "kar", "raha", "rahe", "rahi", "hoga", "apne", "aur", "pe",
    "se", "ko", "par", "ek", "sab", "kuch", "bohot", "bhai", "yaar", "accha",
    "samajh", "aaj", "kal", "kaise", "kyun", "bataye", "baat", "log", "dekho",
    "hamesha", "kabhi", "chahiye", "zaroor", "lekin", "jab", "tab", "ab"
}


def detect_hook_type(first_line: str) -> str:
    """
    Classifies the opening hook style from the first line of text.
    Categories: "question", "number", "bold_claim", "story_opening", "how_to", "other"
    """
    line = first_line.strip()
    if not line:
        return "other"

    lower = line.lower()

    # 1. Question hook
    if "?" in line or any(lower.startswith(w) for w in ["why", "what", "is ", "can ", "do ", "are ", "kya ", "kaise ", "kyun "]):
        return "question"

    # 2. How-to hook
    if any(lower.startswith(w) for w in ["how to", "how i ", "how we ", "kaise kare", "kaise banaye"]):
        return "how_to"

    # 3. Numbered hook (e.g. "5 rules for...", "3 mistakes to avoid")
    if re.match(r"^\d+[\.\)\s]", line) or re.search(r"\b\d+\s+(ways|steps|rules|lessons|tips|reasons|mistakes|tools|secrets|things|habits|books|ideas)\b", lower):
        return "number"

    # 4. Bold claim / controversy / high intrigue
    bold_markers = [
        "never", "always", "stop doing", "stop ", "the truth about", "most people",
        "unpopular opinion", "nobody talks about", "galti mat", "secret to",
        "lie you", "don't ever", "99% of", "har koi", "biggest mistake"
    ]
    if any(m in lower for m in bold_markers):
        return "bold_claim"

    # 5. Story opening
    story_markers = [
        "i was", "last year", "in 202", "in 201", "years ago", "when i", "today i",
        "yesterday i", "ek din", "jab mai", "a few months ago", "my first", "i started"
    ]
    if any(lower.startswith(m) for m in story_markers):
        return "story_opening"

    return "other"


def detect_format(text: str, part_count: int) -> str:
    """
    Classifies the post layout format:
    "single", "thread", "question", "list", "story", "announcement"
    """
    if part_count > 1:
        return "thread"

    lower = text.lower()

    # List check: presence of bullet points or numbered lines
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    bullet_lines = sum(1 for l in lines if l.startswith(("-", "*", "•")) or re.match(r"^\d+[\.\)]\s", l))
    if bullet_lines >= 2:
        return "list"

    # Announcement check
    announcement_markers = [
        "announcing", "excited to announce", "launching", "we just launched",
        "introducing", "new update", "thrilled to share", "big news", "update:"
    ]
    if any(m in lower for m in announcement_markers):
        return "announcement"

    # Story check: narrative progression with multiple paragraphs
    story_markers = ["when i was", "i realized", "then happened", "suddenly", "years ago", "long story short"]
    if len(lines) >= 3 and any(m in lower for m in story_markers):
        return "story"

    # Question check: ends with question mark or prominent question
    if text.strip().endswith("?") or (len(lines) > 0 and lines[-1].endswith("?")):
        return "question"

    return "single"


def detect_language(text: str) -> str:
    """
    Heuristic language guess:
    - "hindi" if significant Devanagari characters
    - "hinglish" if Romanized Hindi words found
    - "english" if predominantly standard English
    - "other" fallback
    """
    devanagari_chars = len(re.findall(r"[\u0900-\u097f]", text))
    if devanagari_chars >= 6 or (len(text) > 0 and devanagari_chars / len(text) > 0.15):
        return "hindi"

    tokens = [w.strip(".,!?:;\"'()[]{}") for w in text.lower().split()]
    hinglish_matches = sum(1 for t in tokens if t in HINGLISH_WORDS)
    if hinglish_matches >= 2:
        return "hinglish"

    # Simple ASCII check for English
    ascii_letters = sum(1 for c in text if c.isascii() and c.isalpha())
    if len(text) > 0 and (ascii_letters / len(text)) > 0.4:
        return "english"

    return "other"


def extract_features_for_part(
    part_text: str,
    part_index: int,
    part_count: int,
    posted_at_utc: datetime,
) -> dict:
    """
    Extracts all feature values for a single chain part or standalone post.
    """
    # Timing in IST
    if posted_at_utc.tzinfo is None:
        posted_at_utc = posted_at_utc.replace(tzinfo=timezone.utc)
    ist_time = posted_at_utc.astimezone(IST)
    posted_hour_ist = ist_time.hour
    posted_weekday_ist = ist_time.strftime("%A")

    # Length metrics
    char_count = len(part_text)
    words = part_text.split()
    word_count = len(words)

    # First line analysis
    lines = [l.strip() for l in part_text.splitlines() if l.strip()]
    first_line = lines[0] if lines else ""
    hook_type = detect_hook_type(first_line)

    # Format
    format_type = detect_format(part_text, part_count)

    # Links, emojis, hashtags
    has_link = bool(URL_PATTERN.search(part_text))
    emojis = EMOJI_PATTERN.findall(part_text)
    has_emoji = len(emojis) > 0
    emoji_count = len(emojis)
    hashtag_count = len(HASHTAG_PATTERN.findall(part_text))

    # Language
    language_guess = detect_language(part_text)

    return {
        "part_index": part_index,
        "char_count": char_count,
        "word_count": word_count,
        "part_count": part_count,
        "format": format_type,
        "hook_type": hook_type,
        "has_link": has_link,
        "has_emoji": has_emoji,
        "emoji_count": emoji_count,
        "hashtag_count": hashtag_count,
        "language_guess": language_guess,
        "posted_hour_ist": posted_hour_ist,
        "posted_weekday_ist": posted_weekday_ist,
    }


def tag_post(db: Session, target: PostTarget) -> List[PostFeature]:
    """
    Extracts and persists features for all parts of a post target.
    Safe to call repeatedly (idempotent upsert on post_target_id + part_index).
    """
    post = target.post
    if not post:
        return []

    # Determine post timestamp (use posted_at, fallback to scheduled_at or current UTC)
    ts = target.posted_at or target.scheduled_at or datetime.now(timezone.utc)

    # Split into chain parts
    parts = split_thread_parts(post.title, post.body)
    part_count = len(parts)

    saved_features: List[PostFeature] = []

    for idx, part_text in enumerate(parts):
        feat_dict = extract_features_for_part(
            part_text=part_text,
            part_index=idx,
            part_count=part_count,
            posted_at_utc=ts,
        )

        # Check existing row
        stmt = select(PostFeature).where(
            PostFeature.post_target_id == target.id,
            PostFeature.part_index == idx,
        )
        existing = db.scalars(stmt).first()

        if existing:
            for k, v in feat_dict.items():
                setattr(existing, k, v)
            saved_features.append(existing)
        else:
            row = PostFeature(
                post_target_id=target.id,
                **feat_dict,
            )
            db.add(row)
            saved_features.append(row)

    db.commit()
    return saved_features


def backfill_all_tags(db: Session) -> int:
    """
    Iterates through all posted Threads targets and computes missing post_features.
    Returns the count of post targets tagged.
    """
    stmt = (
        select(PostTarget)
        .where(
            PostTarget.platform == "threads",
            PostTarget.status == "posted",
        )
        .order_by(PostTarget.id.asc())
    )
    targets = db.scalars(stmt).all()
    tagged_count = 0

    for t in targets:
        try:
            tag_post(db, t)
            tagged_count += 1
        except Exception as exc:
            logger.warning(f"Backfill tagging failed for target #{t.id}: {exc}")

    return tagged_count

