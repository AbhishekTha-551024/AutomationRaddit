"""
Application configuration using pydantic-settings.
Loads environment variables from .env file.
"""

from typing import Optional, List
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Application environment: development, testing, or production
    APP_ENV: str = "development"

    # Database connection string.
    # Default is SQLite locally; can be switched to PostgreSQL via DATABASE_URL
    DATABASE_URL: str = "sqlite:///./scheduler.db"

    # Telegram Bot API credentials (loaded from .env)
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_CHAT_ID: Optional[str] = None

    # Discord Webhook URL (loaded from .env)
    DISCORD_WEBHOOK_URL: Optional[str] = None

    # Threads (Meta) API credentials (loaded from .env)
    THREADS_ACCESS_TOKEN: Optional[str] = None
    THREADS_APP_SECRET: Optional[str] = None

    # Instagram (Meta Graph API) credentials (loaded from .env)
    INSTAGRAM_ACCESS_TOKEN: Optional[str] = None
    INSTAGRAM_APP_SECRET: Optional[str] = None
    INSTAGRAM_APP_ID: Optional[str] = None
    INSTAGRAM_ACCOUNT_ID: Optional[str] = None

    # Fernet encryption key for database token storage (loaded from .env)
    ENCRYPTION_KEY: Optional[str] = None

    # xAI (Grok) configuration for weekly AI Analyst
    XAI_API_KEY: Optional[str] = None
    XAI_MODEL: Optional[str] = None
    AI_MONTHLY_BUDGET_USD: float = 5.0
    ANALYST_MIN_POSTS: int = 20

    # Configurable retry delays in minutes (e.g. "5,15,60" or "1,1,1" for tests)
    RETRY_DELAYS_MINUTES: str = "5,15,60"

    # Grace window in hours before overdue pending posts require manual confirmation
    OVERDUE_GRACE_HOURS: int = 6

    # Failure alerts via Telegram
    ALERTS_ENABLED: bool = True
    TELEGRAM_ALERT_CHAT_ID: Optional[str] = None
    ALERT_QUIET_HOURS_START: Optional[str] = None  # e.g. "23:00" in IST
    ALERT_QUIET_HOURS_END: Optional[str] = None    # e.g. "07:00" in IST
    ALERT_DIGEST_HOUR_IST: int = 9
    BASE_URL: str = "http://127.0.0.1:8000"

    @property
    def retry_delays(self) -> List[int]:
        """Returns the retry delays parsed as a list of integers."""
        try:
            return [int(x.strip()) for x in self.RETRY_DELAYS_MINUTES.split(",") if x.strip()]
        except Exception:
            return [5, 15, 60]

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


# Singleton instance of settings
settings = Settings()
