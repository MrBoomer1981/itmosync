"""The date of the last successful sync.

The daily launchd agent fires every hour and on login, so that the first wake of the day
catches a run launchd postponed while the Mac slept. This stamp is what turns those
frequent firings into one sync a day: a run that finds today's date here does nothing.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Final

from itmosync.logging import get_logger

STAMP_PATH: Final = Path.home() / ".local" / "state" / "itmosync" / "last-sync"

_log = get_logger()


def _resolve(path: Path | None) -> Path:
    # Looked up at call time rather than bound as a default, so tests can redirect it.
    return path if path is not None else STAMP_PATH


def last_synced(path: Path | None = None) -> date | None:
    """The date of the last successful sync, or None if there is none or it is unreadable."""
    try:
        return date.fromisoformat(_resolve(path).read_text("utf-8").strip())
    except (OSError, ValueError):
        return None


def synced_on(day: date, path: Path | None = None) -> bool:
    return last_synced(path) == day


def mark_synced(day: date, path: Path | None = None) -> None:
    """Record a successful sync. A failure here must not fail a sync that already succeeded."""
    target = _resolve(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{day.isoformat()}\n", "utf-8")
    except OSError as exc:
        _log.warning("не удалось записать дату синхронизации в %s: %s", target, exc)
