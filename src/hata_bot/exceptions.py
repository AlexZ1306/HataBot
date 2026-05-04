class HataBotError(Exception):
    """Base exception for HataBot."""


class ConfigError(HataBotError):
    """Raised when configuration is invalid."""


class ProviderError(HataBotError):
    """Raised when a provider request or parse fails."""


class SuspiciousResponseError(ProviderError):
    """Raised when the source response looks incomplete or blocked."""


class NotificationError(HataBotError):
    """Raised when notifications cannot be delivered."""


class SingleInstanceError(HataBotError):
    """Raised when another run is already active."""


class BrowserAutomationError(HataBotError):
    """Raised when a browser-backed fetch strategy fails."""
