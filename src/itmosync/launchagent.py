"""Registering itmosync with launchd.

macOS runs unattended jobs through launchd, which reads a plist from ~/Library/LaunchAgents.
The agent runs the installed `itmosync` binary by absolute path: launchd gives a job a
minimal environment, and a bare command name would not be found.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from itmosync.errors import ConfigError

LABEL: Final = "ru.itmosync.sync"
PLIST_PATH: Final = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_PATH: Final = Path.home() / "Library" / "Logs" / "itmosync.log"

MONTHLY: Final = "monthly"
WEEKLY: Final = "weekly"


@dataclass(frozen=True, slots=True)
class Schedule:
    """When the agent should fire."""

    interval: str = MONTHLY
    hour: int = 9
    minute: int = 0

    def calendar_interval(self) -> dict[str, int]:
        base = {"Hour": self.hour, "Minute": self.minute}
        if self.interval == WEEKLY:
            return {**base, "Weekday": 1}  # Monday
        return {**base, "Day": 1}  # the 1st of each month

    def describe(self) -> str:
        when = f"{self.hour:02d}:{self.minute:02d}"
        if self.interval == WEEKLY:
            return f"по понедельникам в {when}"
        return f"1-го числа каждого месяца в {when}"


def executable_path() -> str:
    """Absolute path to the installed itmosync binary."""
    found = shutil.which("itmosync")
    if found:
        return str(Path(found).resolve())
    fallback = Path.home() / ".local" / "bin" / "itmosync"
    if fallback.exists():
        return str(fallback)
    raise ConfigError(
        "Команда itmosync не найдена в PATH.\n"
        "Установите её: uv tool install --editable . из каталога проекта."
    )


def build_plist(schedule: Schedule, program: str | None = None) -> dict[str, Any]:
    """The launchd job definition."""
    return {
        "Label": LABEL,
        "ProgramArguments": [program or executable_path(), "sync", "--notify"],
        "StartCalendarInterval": schedule.calendar_interval(),
        "RunAtLoad": False,
        "StandardOutPath": str(LOG_PATH),
        "StandardErrorPath": str(LOG_PATH),
        "ProcessType": "Background",
    }


def write_plist(schedule: Schedule, path: Path = PLIST_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(build_plist(schedule), handle)
    return path


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args], capture_output=True, text=True, check=False, timeout=30
    )


def domain() -> str:
    return f"gui/{os.getuid()}"


def install(schedule: Schedule, path: Path = PLIST_PATH) -> Path:
    """Write the plist and (re)register the job."""
    write_plist(schedule, path)
    # bootout first: bootstrap fails if the label is already loaded, and re-running install
    # after changing the interval must be a normal thing to do.
    _launchctl("bootout", f"{domain()}/{LABEL}")
    result = _launchctl("bootstrap", domain(), str(path))
    if result.returncode != 0:
        raise ConfigError(
            "launchd не принял задачу:\n"
            f"{result.stderr.strip() or result.stdout.strip()}\n"
            f"Файл оставлен на месте: {path}"
        )
    return path


def uninstall(path: Path = PLIST_PATH) -> bool:
    """Unregister the job and remove the plist. True if there was anything to remove."""
    _launchctl("bootout", f"{domain()}/{LABEL}")
    if path.exists():
        path.unlink()
        return True
    return False


def status(path: Path = PLIST_PATH) -> tuple[bool, str]:
    """Whether the job is registered, plus what launchd says about it."""
    if not path.exists():
        return False, f"Файл задачи не найден: {path}"
    result = _launchctl("print", f"{domain()}/{LABEL}")
    if result.returncode != 0:
        return False, "Задача не зарегистрирована в launchd."

    details = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(("state =", "last exit code =", "runs =")):
            details.append(stripped)
    return True, "\n".join(details) or "зарегистрирована"
