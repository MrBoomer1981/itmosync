"""Regression tests for the sync command's wiring."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from itmosync import cli
from itmosync.config import render_config, write_config
from itmosync.errors import CalendarNotFoundError
from itmosync.ical.icloud import OwnedEvent

runner = CliRunner()


class StubCalendar:
    """Stands in for ICloudCalendar; records whether anything was written."""

    instances: ClassVar[list[StubCalendar]] = []

    def __init__(self, config: Any, password: str, *, exists: bool = False) -> None:
        self.exists = exists
        self.opened_with: bool | None = None
        self.writes: list[str] = []
        StubCalendar.instances.append(self)

    def __enter__(self) -> StubCalendar:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def connect(self) -> None:
        return None

    def open_calendar(self, *, create: bool = True) -> None:
        self.opened_with = create
        if not self.exists and not create:
            raise CalendarNotFoundError("Календарь «ИТМО · Пары» не найден в iCloud.")

    def list_owned(self, start: dt.date, end: dt.date) -> dict[str, OwnedEvent]:
        return {}

    def create_event(self, event: Any) -> None:
        self.writes.append("create")

    def update_event(self, uid: str, event: Any) -> None:
        self.writes.append("update")

    def delete_event(self, uid: str) -> None:
        self.writes.append("delete")


@pytest.fixture
def wired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, schedule_payload: dict[str, Any]
) -> Path:
    """Point the CLI at stubs so no network or Keychain is involved."""
    StubCalendar.instances.clear()

    class StubAuth:
        def __init__(self, *args: object, **kwargs: object) -> None: ...
        def __enter__(self) -> StubAuth:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def close(self) -> None: ...

    class StubClient(StubAuth):
        def fetch_schedule(self, start: dt.date, end: dt.date) -> dict[str, Any]:
            return schedule_payload

    class StubStore:
        def get_apple_password(self) -> str:
            return "abcd-efgh-ijkl-mnop"

    # Freeze "today" onto the fixture's week. Otherwise the window drifts past it as real
    # time passes and these tests quietly degrade into asserting that nothing happens.
    class FrozenDatetime(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
            return dt.datetime(2026, 9, 7, 9, 0, tzinfo=tz)

    monkeypatch.setattr(cli, "datetime", FrozenDatetime)
    monkeypatch.setattr(cli, "Authenticator", StubAuth)
    monkeypatch.setattr(cli, "ItmoClient", StubClient)
    monkeypatch.setattr(cli, "SecretStore", StubStore)
    monkeypatch.setattr(cli, "ICloudCalendar", StubCalendar)

    config_path = tmp_path / "config.toml"
    write_config(config_path, render_config(apple_id="s@e.com", calendar_name="ИТМО · Пары"))
    return config_path


def test_dry_run_works_before_the_calendar_exists(wired: Path) -> None:
    """The very first run is a dry run, and on a clean account there is no calendar yet."""
    result = runner.invoke(cli.app, ["sync", "--dry-run", "--config", str(wired), "--days", "40"])

    assert result.exit_code == 0, result.output
    assert "ещё нет" in result.output
    assert "Добавлено" in result.output
    assert StubCalendar.instances[0].opened_with is False
    assert StubCalendar.instances[0].writes == []


def test_dry_run_never_writes_even_with_a_calendar(
    wired: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "ICloudCalendar",
        lambda config, password: StubCalendar(config, password, exists=True),
    )

    result = runner.invoke(cli.app, ["sync", "--dry-run", "--config", str(wired), "--days", "40"])

    assert result.exit_code == 0, result.output
    assert StubCalendar.instances[0].opened_with is False
    assert StubCalendar.instances[0].writes == []


def test_real_run_asks_for_the_calendar_to_be_created(wired: Path) -> None:
    result = runner.invoke(cli.app, ["sync", "--config", str(wired), "--days", "40"])

    assert result.exit_code == 0, result.output
    assert StubCalendar.instances[0].opened_with is True
    assert StubCalendar.instances[0].writes.count("create") == 20
