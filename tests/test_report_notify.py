"""Tests for macOS notifications."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from itmosync.config import Config
from itmosync.errors import SafetyGuardError
from itmosync.report import notify, notify_failure
from itmosync.sync import SyncPlan, SyncResult


def result(**kwargs: Any) -> SyncResult:
    plan = SyncPlan(start=dt.date(2026, 9, 13), end=dt.date(2026, 10, 13))
    return SyncResult(plan=plan, **kwargs)


def config_with(notify_on: bool) -> Config:
    return Config.model_validate(
        {"calendar": {"apple_id": "s@e.com"}, "sync": {"notify": notify_on}}
    )


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    recorded: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: Any) -> Any:
        recorded.append(args)
        return None

    monkeypatch.setattr("itmosync.report.subprocess.run", fake_run)
    return recorded


def test_nothing_is_sent_when_notifications_are_off(calls: list[list[str]]) -> None:
    notify(result(created=3), config_with(False))
    notify_failure(SafetyGuardError("что-то"), config_with(False))

    assert calls == []


def test_counts_are_reported(calls: list[list[str]]) -> None:
    notify(result(created=3, updated=1, deleted=0), config_with(True))

    assert len(calls) == 1
    script = calls[0][-1]
    assert "добавлено 3, обновлено 1, удалено 0" in script
    assert "itmosync" in script


def test_dry_run_is_labelled(calls: list[list[str]]) -> None:
    notify(result(created=5, dry_run=True), config_with(True))

    assert "Предпросмотр" in calls[0][-1]


def test_failure_reports_only_the_first_line(calls: list[list[str]]) -> None:
    notify_failure(
        SafetyGuardError("Массовое удаление: 15 из 20\nподробности ниже"), config_with(True)
    )

    script = calls[0][-1]
    assert "Массовое удаление: 15 из 20" in script
    assert "подробности ниже" not in script


def test_quotes_do_not_break_the_applescript(calls: list[list[str]]) -> None:
    notify_failure(SafetyGuardError('календарь "ИТМО" не найден'), config_with(True))

    script = calls[0][-1]
    assert '\\"ИТМО\\"' in script


def test_a_broken_osascript_never_fails_the_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(args: list[str], **kwargs: Any) -> Any:
        raise OSError("osascript отсутствует")

    monkeypatch.setattr("itmosync.report.subprocess.run", boom)

    notify(result(created=1), config_with(True))
