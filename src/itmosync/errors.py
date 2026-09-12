"""Exception hierarchy for itmosync.

Every error carries the process exit code the CLI must return, so that cli.py maps
exceptions to exit codes in exactly one place. Messages are Russian and are expected
to tell the user what to do next, not merely what broke.
"""

from __future__ import annotations

from typing import ClassVar, Final

REFRESH_TOKEN_SNIPPET: Final = (
    "decodeURIComponent(document.cookie.split('auth._refresh_token.itmoId=')[1].split(';')[0])"
)


class ItmosyncError(Exception):
    """Base class for every error itmosync raises deliberately."""

    exit_code: ClassVar[int] = 1


class ConfigError(ItmosyncError):
    """The configuration file is missing, unreadable or invalid."""

    exit_code: ClassVar[int] = 2


class AuthError(ItmosyncError):
    """Authentication against ITMO ID failed."""

    exit_code: ClassVar[int] = 3


class TokenExpiredError(AuthError):
    """The refresh token is dead and a new one has to be taken from the browser."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.instructions())

    @staticmethod
    def instructions() -> str:
        return (
            "Refresh-токен больше не действует, нужен новый.\n\n"
            "  1. Откройте https://my.itmo.ru и войдите в личный кабинет.\n"
            "  2. Откройте консоль браузера (⌥⌘I, вкладка Console) и выполните:\n\n"
            f"     {REFRESH_TOKEN_SNIPPET}\n\n"
            "  3. Скопируйте полученную строку без кавычек и выполните:\n\n"
            "     itmosync auth --set-token\n"
        )


class TokenRejectedError(AuthError):
    """The identity provider refused the token we sent."""


class ItmoApiError(ItmosyncError):
    """The my.itmo schedule API failed or answered with something unusable."""

    exit_code: ClassVar[int] = 4


class ScheduleSchemaError(ItmoApiError):
    """The schedule response did not fit the model."""


class RateLimitError(ItmoApiError):
    """The schedule API asked us to slow down."""


class CalendarError(ItmosyncError):
    """Talking to the calendar failed."""

    exit_code: ClassVar[int] = 5


class CalDavAuthError(CalendarError):
    """iCloud refused the Apple ID or the app-specific password."""


class CalendarNotFoundError(CalendarError):
    """The configured calendar does not exist and could not be created."""


class SafetyGuardError(ItmosyncError):
    """A safety guard stopped the run before anything was written."""

    exit_code: ClassVar[int] = 6
