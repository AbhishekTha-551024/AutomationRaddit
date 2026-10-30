"""
Base interface and result types for platform adapters.
All platform publishers (Telegram, Reddit, Instagram) implement this interface.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Any


@dataclass
class PublishResult:
    """
    Standardized result object returned after attempting to publish a post.
    - success: True if published successfully, False on error
    - external_post_id: ID or URL given by the platform (e.g. Telegram message ID)
    - error_message: Human-readable error description on failure (sanitized, no secrets)
    - retryable: False for permanent client/auth errors (400, 401, 403); True for network, timeout, 429, 5xx.
    """
    success: bool
    external_post_id: Optional[str] = None
    error_message: Optional[str] = None
    retryable: bool = True


@dataclass
class DeleteResult:
    """
    Standardized result object returned after attempting to delete a published post from a platform.
    """
    success: bool
    error_message: Optional[str] = None


class BaseAdapter(ABC):
    """
    Abstract base class for all platform adapters.
    Each platform implements its own `publish` and optional `delete` methods.
    """
    # Capability flag indicating whether the platform supports deleting published posts
    supports_delete: bool = False

    @abstractmethod
    def publish(self, post: Any, target: Any = None) -> PublishResult:
        """
        Publishes a post to the external platform.
        Must catch platform exceptions and return PublishResult(success=False, error_message=...).
        """
        pass

    def delete(self, target: Any) -> DeleteResult:
        """
        Deletes a previously published post from the platform.
        Override in platforms that support remote deletion (Telegram, Discord, Threads).
        """
        return DeleteResult(
            success=False,
            error_message="Remote deletion is not supported for this platform",
        )
