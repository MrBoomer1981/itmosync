"""Regression tests for the sync command's wiring."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from itmosync import cli
from itmosync.config import render_config, write_config
from itmosync.errors import CalendarError, CalendarNotFoundError, NetworkUnavailableError
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
    monkeypatch.setattr(cli, "wait_for_network", lambda: None)

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


TODAY = dt.date(2026, 9, 7)  # FrozenDatetime's date


def test_a_real_run_records_the_day(wired: Path, stamp_path: Path) -> None:
    result = runner.invoke(cli.app, ["sync", "--config", str(wired), "--days", "40"])

    assert result.exit_code == 0, result.output
    assert stamp_path.read_text("utf-8").strip() == TODAY.isoformat()


def test_a_dry_run_does_not_record_the_day(wired: Path, stamp_path: Path) -> None:
    result = runner.invoke(cli.app, ["sync", "--dry-run", "--config", str(wired), "--days", "40"])

    assert result.exit_code == 0, result.output
    assert not stamp_path.exists()


def test_a_failed_run_does_not_record_the_day(
    wired: Path, stamp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenCalendar(StubCalendar):
        def connect(self) -> None:
            raise CalendarError("Не удалось подключиться к iCloud: timeout")

    monkeypatch.setattr(cli, "ICloudCalendar", BrokenCalendar)

    result = runner.invoke(cli.app, ["sync", "--once-a-day", "--config", str(wired)])

    # cli.app is invoked without main(), so the error surfaces as the exception itself.
    assert isinstance(result.exception, CalendarError)
    assert not stamp_path.exists()


def test_once_a_day_skips_when_already_synced_today(
    wired: Path, stamp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stamp_path.parent.mkdir(parents=True)
    stamp_path.write_text(TODAY.isoformat(), "utf-8")
    waited: list[bool] = []
    monkeypatch.setattr(cli, "wait_for_network", lambda: waited.append(True))

    result = runner.invoke(cli.app, ["sync", "--once-a-day", "--config", str(wired)])

    assert result.exit_code == 0, result.output
    assert "уже была" in result.output
    # The skip happens before anything touches the network.
    assert waited == []
    assert StubCalendar.instances == []


def test_once_a_day_syncs_on_a_new_day(wired: Path, stamp_path: Path) -> None:
    stamp_path.parent.mkdir(parents=True)
    stamp_path.write_text((TODAY - dt.timedelta(days=1)).isoformat(), "utf-8")

    result = runner.invoke(
        cli.app, ["sync", "--once-a-day", "--config", str(wired), "--days", "40"]
    )

    assert result.exit_code == 0, result.output
    assert StubCalendar.instances[0].writes.count("create") == 20
    assert stamp_path.read_text("utf-8").strip() == TODAY.isoformat()


def test_no_network_after_wake_fails_quietly(
    wired: Path, stamp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Wi-Fi right after wake is routine; the next hourly firing retries it."""

    def offline() -> None:
        raise NetworkUnavailableError("Сети нет: my.itmo не ответил за 90 с.")

    notified: list[Exception] = []
    monkeypatch.setattr(cli, "wait_for_network", offline)
    monkeypatch.setattr(cli, "notify_failure", lambda exc, config: notified.append(exc))

    result = runner.invoke(cli.app, ["sync", "--once-a-day", "--notify", "--config", str(wired)])

    assert isinstance(result.exception, NetworkUnavailableError)
    assert notified == []
    assert StubCalendar.instances == []
    assert not stamp_path.exists()


@pytest.mark.parametrize("extra", [["--weekly"], ["--hour", "7"]])
def test_daily_rejects_a_fixed_time(extra: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    installed: list[object] = []
    monkeypatch.setattr(cli, "install_agent", installed.append)

    result = runner.invoke(cli.app, ["schedule", "--install", "--daily", *extra])

    assert result.exit_code == 2
    assert "--daily не сочетается" in result.output
    assert installed == []
