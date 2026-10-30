"""
Adapter registry for resolving platform publishers.
Supports Telegram, Discord Webhook, Meta Threads, and Instagram Graph API.
"""

from typing import Set
from app.adapters.base import BaseAdapter
from app.adapters.telegram import TelegramAdapter
from app.adapters.discord import DiscordAdapter
from app.adapters.threads import ThreadsAdapter
from app.adapters.instagram import InstagramAdapter

SUPPORTED_PLATFORMS: Set[str] = {"telegram", "discord", "threads", "instagram"}


def is_platform_supported(platform: str) -> bool:
    """Checks whether the given platform has a registered adapter."""
    return platform.strip().lower() in SUPPORTED_PLATFORMS


def get_adapter(platform: str) -> BaseAdapter:
    """
    Returns an instance of BaseAdapter for the specified platform.
    Raises ValueError with a clear message if the platform is unknown.
    """
    normalized = platform.strip().lower()

    if normalized == "telegram":
        return TelegramAdapter()
    elif normalized == "discord":
        return DiscordAdapter()
    elif normalized == "threads":
        return ThreadsAdapter()
    elif normalized == "instagram":
        return InstagramAdapter()

    raise ValueError(
        f"Unsupported platform '{platform}'. "
        f"Available platform adapters: {sorted(list(SUPPORTED_PLATFORMS))}."
    )

