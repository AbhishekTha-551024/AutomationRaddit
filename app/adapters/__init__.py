"""
Adapters package initialization.
Exports BaseAdapter, PublishResult, TelegramAdapter, DiscordAdapter, and get_adapter.
"""

from app.adapters.base import BaseAdapter, PublishResult
from app.adapters.telegram import TelegramAdapter
from app.adapters.discord import DiscordAdapter
from app.adapters.registry import get_adapter

__all__ = [
    "BaseAdapter",
    "PublishResult",
    "TelegramAdapter",
    "DiscordAdapter",
    "get_adapter",
]
