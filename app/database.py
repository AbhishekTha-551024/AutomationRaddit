"""
Database configuration and session management using SQLAlchemy 2.x.
"""

from typing import Generator
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session
from app.config import settings

# For SQLite, check_same_thread needs to be False for FastAPI multi-threaded requests.
# When switching to PostgreSQL, connect_args is empty.
connect_args = {"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {}

# SQLAlchemy 2.x Engine
engine = create_engine(
    settings.DATABASE_URL,
    connect_args=connect_args,
    echo=False,
)

# Session factory for generating new database sessions per request
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# Base class for all SQLAlchemy Declarative models in SQLAlchemy 2.x
class Base(DeclarativeBase):
    pass


def migrate_db_columns() -> None:
    """
    Checks for schema additions (like 'publish_state' or 'posted_at' on post_targets)
    and executes ALTER TABLE safely without destroying existing user data.
    Also backfills posted_at from logs for previously posted targets.
    """
    inspector = inspect(engine)
    table_names = inspector.get_table_names()

    if "post_targets" in table_names:
        columns = [c["name"] for c in inspector.get_columns("post_targets")]
        if "publish_state" not in columns:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE post_targets ADD COLUMN publish_state TEXT;"))
                conn.commit()

        if "posted_at" not in columns:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE post_targets ADD COLUMN posted_at DATETIME;"))
                conn.commit()

            # Backfill posted_at from execution logs for existing posted targets
            with engine.connect() as conn:
                backfill_sql = text("""
                    UPDATE post_targets
                    SET posted_at = (
                        SELECT created_at FROM logs
                        WHERE logs.post_target_id = post_targets.id
                          AND logs.message LIKE '%Successfully published%'
                        ORDER BY logs.id DESC
                        LIMIT 1
                    )
                    WHERE post_targets.status = 'posted'
                      AND post_targets.posted_at IS NULL;
                """)
                conn.execute(backfill_sql)
                conn.commit()

        if "deleted_at" not in columns:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE post_targets ADD COLUMN deleted_at DATETIME;"))
                conn.commit()

    if "posts" in table_names:
        post_columns = [c["name"] for c in inspector.get_columns("posts")]
        if "media_url" not in post_columns:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE posts ADD COLUMN media_url TEXT;"))
                conn.commit()

        if "edited_at" not in post_columns:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE posts ADD COLUMN edited_at DATETIME;"))
                conn.commit()

        if "deleted_at" not in post_columns:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE posts ADD COLUMN deleted_at DATETIME;"))
                conn.commit()

    # Ensure all models are imported so their tables are registered
    import app.models  # noqa: F401

    # Ensure all tables are created
    Base.metadata.create_all(bind=engine)

    # Initialize default app settings
    with SessionLocal() as db:
        from app.models.setting import AppSetting
        setting = db.get(AppSetting, "publishing_paused")
        if not setting:
            db.add(AppSetting(key="publishing_paused", value="false"))
            db.commit()

        alert_setting = db.get(AppSetting, "alerts_enabled")
        if not alert_setting:
            db.add(AppSetting(key="alerts_enabled", value="true" if settings.ALERTS_ENABLED else "false"))
            db.commit()


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency that yields a database session per request
    and guarantees proper closing when the request finishes.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
