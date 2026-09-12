"""Tests for the launchd job definition.

launchctl itself is never invoked: the plist is the contract, and it can be checked by
parsing it back.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from itmosync.errors import ConfigError
from itmosync.launchagent import (
    LABEL,
    MONTHLY,
    WEEKLY,
    Schedule,
    build_plist,
    executable_path,
    write_plist,
)

PROGRAM = "/Users/student/.local/bin/itmosync"


def test_monthly_schedule_fires_on_the_first() -> None:
    plist = build_plist(Schedule(interval=MONTHLY, hour=9), program=PROGRAM)

    assert plist["StartCalendarInterval"] == {"Day": 1, "Hour": 9, "Minute": 0}
    assert "Weekday" not in plist["StartCalendarInterval"]


def test_weekly_schedule_fires_on_monday() -> None:
    plist = build_plist(Schedule(interval=WEEKLY, hour=7), program=PROGRAM)

    assert plist["StartCalendarInterval"] == {"Weekday": 1, "Hour": 7, "Minute": 0}
    assert "Day" not in plist["StartCalendarInterval"]


def test_the_agent_runs_sync_with_notifications() -> None:
    plist = build_plist(Schedule(), program=PROGRAM)

    assert plist["ProgramArguments"] == [PROGRAM, "sync", "--notify"]
    assert plist["Label"] == LABEL
    # Without --notify an unattended failure would be invisible.
    assert "--notify" in plist["ProgramArguments"]


def test_the_program_path_is_absolute() -> None:
    """launchd gives a job a minimal PATH; a bare command name would never resolve."""
    plist = build_plist(Schedule(), program=PROGRAM)

    assert Path(plist["ProgramArguments"][0]).is_absolute()


def test_it_does_not_run_at_load() -> None:
    """Installing the agent must not trigger a sync as a side effect."""
    assert build_plist(Schedule(), program=PROGRAM)["RunAtLoad"] is False


def test_written_plist_parses_back(tmp_path: Path) -> None:
    path = tmp_path / "agent.plist"

    write_plist(Schedule(interval=WEEKLY, hour=18), path)

    with path.open("rb") as handle:
        parsed = plistlib.load(handle)
    assert parsed["StartCalendarInterval"]["Hour"] == 18
    assert parsed["Label"] == LABEL


def test_missing_binary_explains_how_to_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("itmosync.launchagent.shutil.which", lambda _name: None)
    monkeypatch.setattr("itmosync.launchagent.Path.exists", lambda _self: False)

    with pytest.raises(ConfigError, match="uv tool install"):
        executable_path()


@pytest.mark.parametrize(
    ("interval", "expected"),
    [(MONTHLY, "1-го числа"), (WEEKLY, "по понедельникам")],
)
def test_description_is_human_readable(interval: str, expected: str) -> None:
    assert expected in Schedule(interval=interval).describe()
