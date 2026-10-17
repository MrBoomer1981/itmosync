"""Tests for the launchd job definition.

launchctl itself is never invoked: the plist is the contract, and it can be checked by
parsing it back. The wrapper is shell, so /bin/sh runs it for real, against a stand-in for
itmosync and a stand-in osascript that records notifications instead of showing them.
"""

from __future__ import annotations

import json
import plistlib
import subprocess
from pathlib import Path
from typing import Any

import pytest

from itmosync.errors import ConfigError
from itmosync.launchagent import (
    DAILY,
    LABEL,
    MONTHLY,
    WEEKLY,
    Schedule,
    build_plist,
    executable_path,
    write_plist,
)

PROGRAM = "/Users/student/.local/bin/itmosync"


def _command(plist: dict[str, Any]) -> list[str]:
    """The itmosync invocation inside the wrapper."""
    shell, flag, _script, _name, *command = plist["ProgramArguments"]
    assert (shell, flag) == ("/bin/sh", "-c")
    return command


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

    # Without --notify an unattended failure would be invisible.
    assert _command(plist) == [PROGRAM, "sync", "--notify"]
    assert plist["Label"] == LABEL


def test_the_program_paths_are_absolute() -> None:
    """launchd gives a job a minimal PATH; a bare command name would never resolve."""
    plist = build_plist(Schedule(), program=PROGRAM)

    assert Path(plist["ProgramArguments"][0]).is_absolute()
    assert Path(_command(plist)[0]).is_absolute()


def test_it_does_not_run_at_load() -> None:
    """Installing the agent must not trigger a sync as a side effect."""
    assert build_plist(Schedule(), program=PROGRAM)["RunAtLoad"] is False


def test_written_plist_parses_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Otherwise the result would depend on whichever itmosync happens to be on PATH.
    monkeypatch.setattr("itmosync.launchagent.executable_path", lambda: PROGRAM)
    path = tmp_path / "agent.plist"

    write_plist(Schedule(interval=WEEKLY, hour=18), path)

    with path.open("rb") as handle:
        parsed = plistlib.load(handle)
    assert parsed["StartCalendarInterval"]["Hour"] == 18
    assert parsed["Label"] == LABEL
    assert _command(parsed)[0] == PROGRAM


def test_a_refused_install_leaves_the_current_plist_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening the file truncates it; a refusal must not cost the agent already there."""

    def refuse() -> str:
        raise ConfigError("editable-установка")

    monkeypatch.setattr("itmosync.launchagent.executable_path", refuse)
    path = tmp_path / "agent.plist"
    path.write_bytes(b"the agent that works today")

    with pytest.raises(ConfigError):
        write_plist(Schedule(interval=DAILY), path)

    assert path.read_bytes() == b"the agent that works today"


def test_missing_binary_explains_how_to_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("itmosync.launchagent.shutil.which", lambda _name: None)
    monkeypatch.setattr("itmosync.launchagent.Path.exists", lambda _self: False)

    with pytest.raises(ConfigError, match="uv tool install"):
        executable_path()


def _install(root: Path, *, editable: bool | None) -> Path:
    """A stand-in tool environment: bin/itmosync plus the PEP 610 record an installer
    leaves, or no record at all when `editable` is None."""
    program = root / "bin" / "itmosync"
    program.parent.mkdir(parents=True)
    program.write_text("#!/bin/sh\n", "utf-8")
    if editable is not None:
        dist_info = root / "lib" / "python3.12" / "site-packages" / "itmosync-0.1.0.dist-info"
        dist_info.mkdir(parents=True)
        record = {"url": "file:///Users/student/itmosync", "dir_info": {}}
        if editable:
            record["dir_info"] = {"editable": True}
        (dist_info / "direct_url.json").write_text(json.dumps(record), "utf-8")
    return program


def test_an_editable_install_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An editable install stops importing once the project folder moves — silently."""
    program = _install(tmp_path / "env", editable=True)
    monkeypatch.setattr("itmosync.launchagent.shutil.which", lambda _name: str(program))

    with pytest.raises(ConfigError, match="uv tool install --reinstall"):
        executable_path()


def test_a_link_is_judged_by_where_it_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uv puts a symlink into ~/.local/bin; the environment is wherever it points."""
    program = _install(tmp_path / "env", editable=True)
    link = tmp_path / "bin" / "itmosync"
    link.parent.mkdir()
    link.symlink_to(program)
    monkeypatch.setattr("itmosync.launchagent.shutil.which", lambda _name: str(link))

    with pytest.raises(ConfigError, match="editable"):
        executable_path()


@pytest.mark.parametrize("editable", [False, None])
def test_a_standalone_install_is_accepted(
    editable: bool | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program = _install(tmp_path / "env", editable=editable)
    monkeypatch.setattr("itmosync.launchagent.shutil.which", lambda _name: str(program))

    assert executable_path() == str(program.resolve())


def _stand_in(tmp_path: Path, exit_code: int) -> Path:
    program = tmp_path / "itmosync"
    program.write_text(f"#!/bin/sh\nexit {exit_code}\n", "utf-8")
    program.chmod(0o755)
    return program


def _run_agent(tmp_path: Path, program: Path) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run the plist's ProgramArguments as launchd would, with a minimal PATH.

    Returns the process and whatever notifications the stand-in osascript was asked to show.
    """
    shown = tmp_path / "notifications.txt"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    osascript = fake_bin / "osascript"
    osascript.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{shown}"\n', "utf-8")
    osascript.chmod(0o755)

    arguments = build_plist(Schedule(interval=DAILY), program=str(program))["ProgramArguments"]
    result = subprocess.run(
        arguments,
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return result, shown.read_text("utf-8") if shown.exists() else ""


@pytest.mark.parametrize("code", [0, 3, 4, 5, 6])
def test_the_wrapper_stays_quiet_when_itmosync_spoke_for_itself(code: int, tmp_path: Path) -> None:
    """Success, a failure itmosync has notified about, or the deliberately quiet no-network."""
    result, shown = _run_agent(tmp_path, _stand_in(tmp_path, code))

    assert result.returncode == code
    assert shown == ""


@pytest.mark.parametrize("code", [1, 2])
def test_the_wrapper_notifies_when_itmosync_could_not(code: int, tmp_path: Path) -> None:
    """1 is a crash, including an install that no longer imports; 2 is a config that could
    not be read before itmosync knew whether to notify."""
    result, shown = _run_agent(tmp_path, _stand_in(tmp_path, code))

    assert result.returncode == code
    assert f"(код {code})" in shown
    assert "~/Library/Logs/itmosync.log" in shown
    assert f"завершился с кодом {code}" in result.stderr


def test_the_wrapper_notifies_when_the_binary_is_gone(tmp_path: Path) -> None:
    result, shown = _run_agent(tmp_path, tmp_path / "removed" / "itmosync")

    assert result.returncode == 127
    assert "(код 127)" in shown


@pytest.mark.parametrize(
    ("interval", "expected"),
    [(MONTHLY, "1-го числа"), (WEEKLY, "по понедельникам"), (DAILY, "пробуждении")],
)
def test_description_is_human_readable(interval: str, expected: str) -> None:
    assert expected in Schedule(interval=interval).describe()


def test_daily_schedule_fires_every_hour_and_at_login() -> None:
    """Hourly so that launchd replays the firing missed during sleep on wake; at login so
    that a Mac which was shut down does not wait for the next hour."""
    plist = build_plist(Schedule(interval=DAILY), program=PROGRAM)

    assert plist["StartCalendarInterval"] == {"Minute": 0}
    assert plist["RunAtLoad"] is True


def test_daily_schedule_syncs_once_a_day() -> None:
    plist = build_plist(Schedule(interval=DAILY), program=PROGRAM)

    assert _command(plist) == [PROGRAM, "sync", "--notify", "--once-a-day"]


def test_weekly_schedule_keeps_plain_sync() -> None:
    plist = build_plist(Schedule(interval=WEEKLY), program=PROGRAM)

    assert "--once-a-day" not in _command(plist)
    assert plist["RunAtLoad"] is False
