"""The mandatory test set from §13 of the spec.

The whole project exists so that `test_idempotent_second_run` passes: a repeated sync on
unchanged data must produce an empty plan.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from typing import Any

import pytest
from icalendar import Event

from itmosync.config import DEFAULT_LESSON_TYPES, Config
from itmosync.errors import SafetyGuardError
from itmosync.ical.mapper import uid_for
from itmosync.itmo.schedule import parse_schedule
from itmosync.models import Lesson
from itmosync.sync import apply_plan, build_plan, check_guards
from tests.fakes import FakeCalendar

START = dt.date(2026, 9, 7)
END = dt.date(2026, 9, 13)


@pytest.fixture
def lessons(schedule_payload: dict[str, Any]) -> list[Lesson]:
    return parse_schedule(schedule_payload, DEFAULT_LESSON_TYPES)


def run_sync(
    lessons: list[Lesson],
    backend: FakeCalendar,
    config: Config,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> Any:
    existing = backend.list_owned(START, END)
    plan = build_plan(lessons, existing, start=START, end=END)
    check_guards(plan, config, force=force)
    return apply_plan(plan, backend, config, dry_run=dry_run)


def foreign_event(uid: str, when: dt.datetime) -> Event:
    event = Event()
    event.add("uid", uid)
    event.add("summary", "Зубной")
    event.add("dtstart", when)
    event.add("dtend", when + dt.timedelta(hours=1))
    return event


def test_idempotent_second_run(lessons: list[Lesson], config: Config) -> None:
    """The key test of the project."""
    backend = FakeCalendar()

    first = run_sync(lessons, backend, config)
    second = run_sync(lessons, backend, config)

    assert (first.created, first.updated, first.deleted) == (20, 0, 0)
    assert (second.created, second.updated, second.deleted) == (0, 0, 0)
    assert second.plan.is_empty
    assert backend.write_count == 20


def test_third_run_still_changes_nothing(lessons: list[Lesson], config: Config) -> None:
    backend = FakeCalendar()
    for _ in range(3):
        result = run_sync(lessons, backend, config)

    assert result.plan.is_empty
    assert len(backend.events) == 20


def test_room_change_is_update(lessons: list[Lesson], config: Config) -> None:
    backend = FakeCalendar()
    run_sync(lessons, backend, config)

    moved = [replace(lesson, room="2301") if lesson is lessons[3] else lesson for lesson in lessons]
    result = run_sync(moved, backend, config)

    assert (result.created, result.updated, result.deleted) == (0, 1, 0)
    assert result.plan.to_update[0].uid == uid_for(lessons[3])
    assert "→" in result.plan.to_update[0].describe(config)


def test_time_change_is_update_with_the_same_uid(lessons: list[Lesson], config: Config) -> None:
    backend = FakeCalendar()
    run_sync(lessons, backend, config)
    original = lessons[5]

    shifted = [
        replace(lesson, start=dt.time(16, 0), end=dt.time(17, 30)) if lesson is original else lesson
        for lesson in lessons
    ]
    result = run_sync(shifted, backend, config)

    assert (result.created, result.updated, result.deleted) == (0, 1, 0)
    change = result.plan.to_update[0]
    assert change.uid == uid_for(original)
    assert change.describe(config) == (f"{original.start:%H:%M}–{original.end:%H:%M} → 16:00–17:30")


def test_teacher_change_is_named_in_the_report(lessons: list[Lesson], config: Config) -> None:
    backend = FakeCalendar()
    run_sync(lessons, backend, config)
    target = next(lesson for lesson in lessons if lesson.teacher)

    changed = [
        replace(lesson, teacher="Новиков Н.Н.") if lesson is target else lesson
        for lesson in lessons
    ]
    result = run_sync(changed, backend, config)

    assert result.updated == 1
    assert result.plan.to_update[0].describe(config) == "преподаватель изменён"


def test_cancelled_lesson_is_deleted(lessons: list[Lesson], config: Config) -> None:
    backend = FakeCalendar()
    run_sync(lessons, backend, config)

    result = run_sync(lessons[1:], backend, config)

    assert (result.created, result.updated, result.deleted) == (0, 0, 1)
    assert uid_for(lessons[0]) not in backend.events


def test_foreign_event_untouched(lessons: list[Lesson], config: Config) -> None:
    backend = FakeCalendar()
    backend.seed(foreign_event("dentist-2026@icloud.com", dt.datetime(2026, 9, 8, 12, 0)))

    for _ in range(3):
        result = run_sync(lessons, backend, config)

    assert "dentist-2026@icloud.com" in backend.events
    assert result.plan.is_empty
    assert "dentist-2026@icloud.com" not in backend.deleted
    assert "dentist-2026@icloud.com" not in backend.updated


def test_empty_api_response_guard(lessons: list[Lesson], config: Config) -> None:
    backend = FakeCalendar()
    run_sync(lessons, backend, config)
    writes_before = backend.write_count

    with pytest.raises(SafetyGuardError, match="пустое расписание"):
        run_sync([], backend, config)

    assert backend.write_count == writes_before
    assert len(backend.events) == 20


def test_empty_api_response_passes_with_force(lessons: list[Lesson], config: Config) -> None:
    backend = FakeCalendar()
    run_sync(lessons, backend, config)

    result = run_sync([], backend, config, force=True)

    assert result.deleted == 20
    assert backend.events == {}


def test_empty_calendar_and_empty_api_is_not_a_guard(config: Config) -> None:
    backend = FakeCalendar()

    result = run_sync([], backend, config)

    assert result.plan.is_empty


def test_mass_delete_guard(lessons: list[Lesson], config: Config) -> None:
    backend = FakeCalendar()
    run_sync(lessons, backend, config)

    with pytest.raises(SafetyGuardError, match="Массовое удаление") as excinfo:
        run_sync(lessons[:5], backend, config)

    assert "15 из 20" in str(excinfo.value)
    assert len(backend.events) == 20


def test_deletion_below_threshold_proceeds(lessons: list[Lesson], config: Config) -> None:
    backend = FakeCalendar()
    run_sync(lessons, backend, config)

    result = run_sync(lessons[:15], backend, config)

    assert result.deleted == 5


def test_dry_run_writes_nothing(lessons: list[Lesson], config: Config) -> None:
    backend = FakeCalendar()

    result = run_sync(lessons, backend, config, dry_run=True)

    assert result.dry_run
    assert result.created == 20
    assert backend.write_count == 0
    assert backend.events == {}


def test_duplicate_lessons_same_day_get_two_events(lessons: list[Lesson], config: Config) -> None:
    backend = FakeCalendar()
    run_sync(lessons, backend, config)

    same_day = [
        uid
        for uid, event in backend.events.items()
        if event.decoded("dtstart").date() == dt.date(2026, 9, 12)
    ]

    assert len(same_day) == 2
    assert len(set(same_day)) == 2


def test_lessons_outside_the_window_are_ignored(lessons: list[Lesson], config: Config) -> None:
    backend = FakeCalendar()
    far_away = replace(lessons[0], date=dt.date(2026, 12, 1))

    result = run_sync([*lessons, far_away], backend, config)

    assert result.created == 20


def test_plan_is_ordered_by_date(lessons: list[Lesson], config: Config) -> None:
    plan = build_plan(lessons, {}, start=START, end=END)

    dates = [(lesson.date, lesson.start) for lesson in plan.to_create]

    assert dates == sorted(dates)


def test_create_runs_before_delete(lessons: list[Lesson], config: Config) -> None:
    """A lesson that moves to a new identity must never leave a gap in the calendar."""
    backend = FakeCalendar()
    run_sync(lessons[:10], backend, config)

    renamed = [replace(lesson, subject=lesson.subject + " (перенос)") for lesson in lessons[:10]]
    run_sync(renamed, backend, config, force=True)

    assert backend.created[-10:] == [
        uid_for(lesson) for lesson in sorted(renamed, key=lambda item: (item.date, item.start))
    ]
    assert len(backend.deleted) == 10
    assert len(backend.events) == 10


def test_link_change_without_visible_fields_is_still_an_update(lessons: list[Lesson]) -> None:
    """Nothing visible moved, but the content hash did — the event must still be updated."""
    config = Config.model_validate(
        {"calendar": {"apple_id": "s@e.com"}, "sync": {"online_link_in_location": False}}
    )
    backend = FakeCalendar()
    run_sync(lessons, backend, config)
    online = next(lesson for lesson in lessons if lesson.is_online)

    relinked = [
        replace(lesson, online_url="https://itmo.zoom.us/j/9999999999")
        if lesson is online
        else lesson
        for lesson in lessons
    ]
    result = run_sync(relinked, backend, config)

    assert result.updated == 1
    assert result.plan.to_update[0].describe(config) == "изменены детали"


def test_teacher_removal_is_named(lessons: list[Lesson], config: Config) -> None:
    backend = FakeCalendar()
    run_sync(lessons, backend, config)
    target = next(lesson for lesson in lessons if lesson.teacher)

    without = [replace(lesson, teacher=None) if lesson is target else lesson for lesson in lessons]
    result = run_sync(without, backend, config)

    assert result.plan.to_update[0].describe(config) == "преподаватель снят"
