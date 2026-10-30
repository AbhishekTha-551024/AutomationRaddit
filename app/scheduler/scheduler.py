"""
Background scheduler configuration using APScheduler.
Polls the database every 60 seconds for due posts and handles startup crash recovery.
"""

import logging
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select
from app.config import settings
from app.database import SessionLocal
from app.models import PostTarget, Log, TargetStatus
from app.services.publisher import run_due_posts

logger = logging.getLogger(__name__)

# In-process background scheduler instance
scheduler = BackgroundScheduler()


def poll_and_dispatch_job():
    """
    Periodic job invoked every 60 seconds.
    Creates a dedicated database session, executes due posts, and cleans up.
    """
    db = SessionLocal()
    try:
        run_due_posts(db)
    except Exception as exc:
        logger.error(f"Scheduler job encountered an error: {exc}")
    finally:
        db.close()


def recover_orphaned_jobs():
    """
    Startup recovery: If the app restarted or crashed while a post was in 'processing',
    reset its status to 'pending' so it can be re-evaluated, and log the recovery event.
    """
    db = SessionLocal()
    try:
        stmt = select(PostTarget).where(PostTarget.status == TargetStatus.processing)
        orphaned_targets = db.scalars(stmt).all()

        if orphaned_targets:
            for target in orphaned_targets:
                target.status = TargetStatus.pending
                db.add(Log(
                    post_target_id=target.id,
                    level="WARNING",
                    message="App restart detected incomplete job in 'processing' status. Recovered back to 'pending'.",
                ))
            db.commit()
            logger.info(f"Crash recovery: reset {len(orphaned_targets)} orphaned target(s) back to 'pending'.")
    except Exception as exc:
        db.rollback()
        logger.error(f"Failed to run startup crash recovery: {exc}")
    finally:
        db.close()


def daily_threads_token_refresh_job():
    """
    Daily background job to ensure the Threads long-lived access token stays fresh.
    Automatically refreshes the token when fewer than 10 days remain before expiry.
    """
    db = SessionLocal()
    try:
        from app.services.threads_token_service import ensure_fresh_token
        ensure_fresh_token(db)
        logger.info("Daily Threads token refresh check completed successfully.")
    except Exception as exc:
        logger.warning(f"Daily Threads token refresh check: {exc}")
    finally:
        db.close()


def collect_metrics_periodic_job():
    """
    Periodic job invoked every 15 minutes to capture due age bucket snapshots (1h, 6h, 24h, 72h, 7d)
    for posted Threads posts. Runs on its own dedicated DB session.
    """
    db = SessionLocal()
    try:
        from app.services.metrics_collector import collect_due_snapshots
        count = collect_due_snapshots(db)
        if count > 0:
            logger.info(f"Collected {count} new engagement snapshot(s).")
    except Exception as exc:
        logger.error(f"Periodic metrics collection job error: {exc}")
    finally:
        db.close()


def collect_daily_account_snapshot_job():
    """
    Daily background job to capture high-level account performance and audience metrics.
    Runs on its own dedicated DB session.
    """
    db = SessionLocal()
    try:
        from app.services.metrics_collector import collect_account_snapshot
        collect_account_snapshot(db)
        logger.info("Daily account snapshot collection completed.")
    except Exception as exc:
        logger.warning(f"Daily account metrics collection notice: {exc}")
    finally:
        db.close()


def weekly_ai_analyst_job():
    """
    Weekly background job running every Sunday at 09:00 IST (03:30 UTC).
    Synthesizes empirical findings, tracks budget, and proposes Playbook rules.
    """
    db = SessionLocal()
    try:
        from app.services.analyst import run_ai_analyst
        result = run_ai_analyst(db)
        logger.info(f"Weekly AI Analyst job executed: {result.get('status')} - {result.get('message') or result.get('summary')}")
    except Exception as exc:
        logger.error(f"Weekly AI Analyst job exception: {exc}")
    finally:
        db.close()


def daily_purge_expired_trash_job():
    """
    Daily background job to permanently remove soft-deleted posts and targets older than 30 days.
    """
    db = SessionLocal()
    try:
        from app.services.post_operations import purge_expired_trash
        count = purge_expired_trash(db, days=30)
        if count > 0:
            logger.info(f"Daily trash purge: permanently removed {count} expired item(s).")
    except Exception as exc:
        logger.error(f"Daily trash purge job error: {exc}")
    finally:
        db.close()


def daily_connection_tests_job():
    """Daily check running read-only diagnostic tests on all connections."""
    db = SessionLocal()
    try:
        from app.services.connection_service import run_all_connection_tests
        run_all_connection_tests(db)
        logger.info("Daily connection health check completed.")
    except Exception as exc:
        logger.warning(f"Daily connection health check notice: {exc}")
    finally:
        db.close()


def alert_health_monitoring_job():
    """Periodic check for token expiries, paused queue duration, and missed posts."""
    db = SessionLocal()
    try:
        from app.services.alerts import check_token_expiries, check_paused_and_missed_posts, check_ai_budget_alert
        check_token_expiries(db)
        check_paused_and_missed_posts(db)
        check_ai_budget_alert(db)
    except Exception as exc:
        logger.warning(f"Alert health monitoring notice: {exc}")
    finally:
        db.close()


def daily_digest_scheduler_job():
    """Daily digest sent at ALERT_DIGEST_HOUR_IST to the user's private Telegram chat."""
    db = SessionLocal()
    try:
        from app.services.alerts import send_daily_digest
        send_daily_digest(db)
        logger.info("Daily digest check completed.")
    except Exception as exc:
        logger.warning(f"Daily digest job notice: {exc}")
    finally:
        db.close()


def start_scheduler():
    """
    Recovers orphaned tasks and starts the APScheduler runner on app startup.
    """
    recover_orphaned_jobs()

    scheduler.add_job(
        poll_and_dispatch_job,
        trigger="interval",
        seconds=60,
        id="poll_due_posts",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    scheduler.add_job(
        daily_threads_token_refresh_job,
        trigger="interval",
        hours=24,
        id="daily_threads_token_refresh",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    scheduler.add_job(
        collect_metrics_periodic_job,
        trigger="interval",
        minutes=15,
        id="collect_threads_metrics",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    scheduler.add_job(
        collect_daily_account_snapshot_job,
        trigger="interval",
        hours=24,
        id="collect_threads_account_metrics",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    scheduler.add_job(
        weekly_ai_analyst_job,
        trigger="cron",
        day_of_week="sun",
        hour=3,
        minute=30,  # 03:30 UTC = 09:00 IST
        id="weekly_ai_analyst",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    scheduler.add_job(
        daily_purge_expired_trash_job,
        trigger="interval",
        hours=24,
        id="daily_purge_expired_trash",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    scheduler.add_job(
        daily_connection_tests_job,
        trigger="interval",
        hours=24,
        id="daily_connection_tests",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    scheduler.add_job(
        alert_health_monitoring_job,
        trigger="interval",
        minutes=30,
        id="alert_health_monitoring",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    # Convert ALERT_DIGEST_HOUR_IST to UTC (subtract 5.5 hours)
    digest_hour_ist = getattr(settings, "ALERT_DIGEST_HOUR_IST", 9)
    # E.g. 9 IST -> 3:30 UTC
    total_minutes = int(digest_hour_ist * 60 - 330) % (24 * 60)
    utc_hour = total_minutes // 60
    utc_minute = total_minutes % 60

    scheduler.add_job(
        daily_digest_scheduler_job,
        trigger="cron",
        hour=utc_hour,
        minute=utc_minute,
        id="daily_telegram_digest",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    scheduler.start()
    logger.info("APScheduler started (dispatch every 60s, metrics every 15m, connections daily, alerts monitoring every 30m).")


def shutdown_scheduler():
    """
    Gracefully shuts down APScheduler on application termination.
    """
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped.")

