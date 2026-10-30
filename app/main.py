"""
FastAPI application factory and entry point.
Creates database tables on startup, starts the APScheduler engine,
mounts static assets, and registers web views and API routers.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.config import settings
from app.database import Base, engine, migrate_db_columns
import app.models  # noqa: F401
from app.scheduler.scheduler import start_scheduler, shutdown_scheduler
from app.api.posts import router as posts_router
from app.api.targets import router as targets_router
from app.api.logs import router as logs_router
from app.api.stats import router as stats_router
from app.api.web import router as web_router
from app.api.analytics import router as analytics_router
from app.api.playbook import router as playbook_router
from app.api.trash import router as trash_router
from app.api.bulk import router as bulk_router
from app.api.queue import router as queue_router
from app.api.connections import router as connections_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan handler.
    1. Runs database column migrations and creates tables.
    2. Runs crash recovery and starts APScheduler.
    3. Shuts down APScheduler on app termination.
    """
    migrate_db_columns()
    Base.metadata.create_all(bind=engine)
    start_scheduler()

    yield

    shutdown_scheduler()


def create_app() -> FastAPI:
    """
    Application factory.
    """
    app = FastAPI(
        title="Social Media Post Scheduler",
        description="Local-first social media post scheduling and dispatch service.",
        version="0.5.0",
        lifespan=lifespan,
    )

    # Mount static assets (CSS, JS)
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    # Health check endpoint
    @app.get("/health")
    async def health_check():
        """Health check endpoint to verify that the API server is operational."""
        return {"status": "ok"}

    # Register Web UI HTML router
    app.include_router(web_router)

    # Register Core JSON API routers
    app.include_router(posts_router)
    app.include_router(targets_router)
    app.include_router(logs_router)
    app.include_router(stats_router)
    app.include_router(analytics_router)
    app.include_router(playbook_router)
    app.include_router(trash_router)
    app.include_router(bulk_router)
    app.include_router(queue_router)
    app.include_router(connections_router)

    # Register dev-only routes if running in development mode
    if settings.APP_ENV == "development":
        from app.api.dev import router as dev_router
        app.include_router(dev_router)

    return app


# Instantiate the FastAPI application
app = create_app()
