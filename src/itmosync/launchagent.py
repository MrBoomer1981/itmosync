"""Registering itmosync with launchd.

macOS runs unattended jobs through launchd, which reads a plist from ~/Library/LaunchAgents.
The agent runs the installed `itmosync` binary by absolute path: launchd gives a job a
minimal environment, and a bare command name would not be found.

Two things keep the agent from failing in silence. The binary must be a standalone install:
an editable one is tied to the project folder and stops importing once the folder moves.
And launchd runs itmosync inside a small shell wrapper that posts a notification about any
failure itmosync could not report itself.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from itmosync.errors import (
    AuthError,
    CalendarError,
    ConfigError,
    ItmoApiError,
    SafetyGuardError,
)

LABEL: Final = "ru.itmosync.sync"
PLIST_PATH: Final = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_PATH: Final = Path.home() / "Library" / "Logs" / "itmosync.log"

MONTHLY: Final = "monthly"
WEEKLY: Final = "weekly"
DAILY: Final = "daily"

SHELL: Final = "/bin/sh"
# What `sh -c` exposes as $0: the name the shell puts in its own error messages.
WRAPPER_NAME: Final = "itmosync-agent"

# Exit codes the wrapper keeps quiet about. 0 is success. After the others itmosync has
# already posted its own notification, or, for a missing network under --once-a-day,
# stayed quiet on purpose. Any other code means it never got that far: 1 is a crash,
# including an install that no longer imports; 2 is a config it could not read before it
# knew whether to notify; 126 and 127 mean the binary did not start at all.
QUIET_EXIT_CODES: Final = (
    0,
    AuthError.exit_code,
    ItmoApiError.exit_code,
    CalendarError.exit_code,
    SafetyGuardError.exit_code,
)

_WRAPPER_TEMPLATE: Final = """\
"$@"
code=$?
case $code in
  {quiet}) ;;
  *)
    echo "itmosync: автозапуск завершился с кодом $code" >&2
    message="Автозапуск не сработал (код $code), календарь не обновлён. Подробности: {log}"
    osascript -e "display notification \\"$message\\" with title \\"itmosync — ошибка\\""
    ;;
esac
exit $code
"""


def wrapper_script() -> str:
    """The shell script launchd runs: the sync command, then a notification if it failed
    in a way itmosync could not report itself."""
    return _WRAPPER_TEMPLATE.format(
        quiet="|".join(str(code) for code in QUIET_EXIT_CODES),
        log=f"~/{LOG_PATH.relative_to(Path.home())}",
    )


@dataclass(frozen=True, slots=True)
class Schedule:
    """When the agent should fire."""

    interval: str = MONTHLY
    hour: int = 9
    minute: int = 0

    def calendar_interval(self) -> dict[str, int]:
        if self.interval == DAILY:
            # Every hour, on the hour. launchd runs a firing missed during sleep on wake,
            # which is what makes the first wake of the day trigger the sync; the
            # --once-a-day stamp turns the remaining firings into no-ops.
            return {"Minute": 0}
        base = {"Hour": self.hour, "Minute": self.minute}
        if self.interval == WEEKLY:
            return {**base, "Weekday": 1}  # Monday
        return {**base, "Day": 1}  # the 1st of each month

    @property
    def runs_at_load(self) -> bool:
        """Daily mode also fires on login, so a Mac that was shut down rather than asleep
        does not wait for the next hour."""
        return self.interval == DAILY

    def sync_command(self, program: str) -> list[str]:
        """The itmosync invocation itself."""
        command = [program, "sync", "--notify"]
        if self.interval == DAILY:
            command.append("--once-a-day")
        return command

    def program_arguments(self, program: str) -> list[str]:
        """What launchd runs: the sync command inside the wrapper.

        `sh -c SCRIPT NAME ARGS...` makes NAME the script's $0 and ARGS its "$@".
        """
        return [SHELL, "-c", wrapper_script(), WRAPPER_NAME, *self.sync_command(program)]

    def describe(self) -> str:
        if self.interval == DAILY:
            return "раз в день, при первом пробуждении Mac"
        when = f"{self.hour:02d}:{self.minute:02d}"
        if self.interval == WEEKLY:
            return f"по понедельникам в {when}"
        return f"1-го числа каждого месяца в {when}"


def _is_editable(program: Path) -> bool:
    """Whether the environment behind `program` runs itmosync straight from a source folder.

    Installers record that in direct_url.json next to the package (PEP 610). An editable
    install remembers the folder's absolute path: move or delete the folder and every run
    dies on `import itmosync`, before itmosync could tell anyone.
    """
    pattern = "lib/python*/site-packages/itmosync-*.dist-info/direct_url.json"
    for record in program.parent.parent.glob(pattern):
        try:
            info = json.loads(record.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        dir_info = info.get("dir_info") if isinstance(info, dict) else None
        if isinstance(dir_info, dict) and dir_info.get("editable") is True:
            return True
    return False


def executable_path() -> str:
    """Absolute path to the installed itmosync binary, which must be a standalone install."""
    found = shutil.which("itmosync")
    candidate = Path(found) if found else Path.home() / ".local" / "bin" / "itmosync"
    if not candidate.exists():
        raise ConfigError(
            "Команда itmosync не найдена в PATH. Установите её из каталога проекта:\n\n"
            "  uv tool install .\n"
        )
    program = candidate.resolve()
    if _is_editable(program):
        raise ConfigError(
            f"{program} запускает itmosync прямо из папки проекта (editable-установка).\n"
            "Агенту нужна отдельная копия, иначе перенос или удаление папки молча сломает\n"
            "автозапуск. Поставьте её из каталога проекта:\n\n"
            "  uv tool install --reinstall .\n\n"
            "и повторите schedule --install установленной командой itmosync, не через uv run."
        )
    return str(program)


def build_plist(schedule: Schedule, program: str | None = None) -> dict[str, Any]:
    """The launchd job definition."""
    return {
        "Label": LABEL,
        "ProgramArguments": schedule.program_arguments(program or executable_path()),
        "StartCalendarInterval": schedule.calendar_interval(),
        "RunAtLoad": schedule.runs_at_load,
        "StandardOutPath": str(LOG_PATH),
        "StandardErrorPath": str(LOG_PATH),
        "ProcessType": "Background",
    }


def write_plist(schedule: Schedule, path: Path = PLIST_PATH) -> Path:
    # Built before the file is opened: opening truncates it, and a refused install must
    # leave the agent that is already there intact.
    plist = build_plist(schedule)
    path.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(plist, handle)
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
