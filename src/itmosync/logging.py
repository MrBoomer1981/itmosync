"""Logging setup with mandatory secret masking.

Tokens must never reach a log file, a terminal or an error message. Rather than
trusting every call site to remember that, the masking happens in a logging filter
that every itmosync logger carries.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final

from rich.logging import RichHandler

LOGGER_NAME: Final = "itmosync"

# A JWT, or any long opaque run of token characters. Dots and slashes are excluded from
# the generic alternative on purpose: without that, filesystem paths and URLs would be
# mangled into unreadable ellipses and the logs would lose their debugging value.
_SECRET_RE: Final = re.compile(
    r"ey[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]+){1,2}"  # JWT
    r"|\b[A-Za-z0-9_-]{21,}\b"  # long opaque token
    r"|\b[a-z]{4}-[a-z]{4}-[a-z]{4}-[a-z]{4}\b"  # Apple app-specific password
)


def _replace(match: re.Match[str]) -> str:
    secret = match.group(0)
    return f"{secret[:4]}…{secret[-4:]}"


def mask_secrets(text: str) -> str:
    """Replace anything that looks like a token with `abcd…wxyz`."""
    return _SECRET_RE.sub(_replace, text)


class SecretMaskingFilter(logging.Filter):
    """Masks secrets in the message and in every string argument of a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask_secrets(str(record.msg))
        args: Any = record.args
        if isinstance(args, tuple):
            record.args = tuple(mask_secrets(a) if isinstance(a, str) else a for a in args)
        elif isinstance(args, dict):
            record.args = {
                key: mask_secrets(value) if isinstance(value, str) else value
                for key, value in args.items()
            }
        return True


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure the itmosync logger and return it."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    logger.handlers.clear()

    # The filter belongs on the logger, not on the handler: anything attached later —
    # a file handler, a test handler — must be covered too.
    logger.filters.clear()
    logger.addFilter(SecretMaskingFilter())
    logger.addHandler(RichHandler(show_time=False, show_path=verbose, rich_tracebacks=True))
    return logger


def get_logger() -> logging.Logger:
    """Return the itmosync logger, configuring it on first use."""
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        return setup_logging()
    return logger
