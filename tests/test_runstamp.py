"""Tests for the last-successful-sync stamp behind `sync --once-a-day`."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from itmosync.runstamp import last_synced, mark_synced, synced_on

TODAY = dt.date(2026, 9, 7)


def test_no_stamp_means_not_synced(stamp_path: Path) -> None:
    assert last_synced() is None
    assert not synced_on(TODAY)


def test_a_sync_today_is_remembered(stamp_path: Path) -> None:
    mark_synced(TODAY)

    assert stamp_path.read_text("utf-8").strip() == "2026-09-07"
    assert synced_on(TODAY)


def test_yesterdays_sync_does_not_count_today() -> None:
    mark_synced(TODAY - dt.timedelta(days=1))

    assert not synced_on(TODAY)


def test_a_garbled_stamp_is_treated_as_no_sync(stamp_path: Path) -> None:
    stamp_path.parent.mkdir(parents=True)
    stamp_path.write_text("not a date", "utf-8")

    assert last_synced() is None


def test_an_unwritable_stamp_does_not_fail_the_sync(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("", "utf-8")

    mark_synced(TODAY, blocker / "last-sync")  # the parent is a file: mkdir fails

    assert last_synced(blocker / "last-sync") is None
