"""Tests for the calendar backend contract and the UID guard."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from icalendar import Event

from itmosync.config import DEFAULT_LESSON_TYPES, Config
from itmosync.errors import CalendarError
from itmosync.ical.icloud import ICloudCalendar, OwnedEvent, _guard_owned
from itmosync.ical.mapper import content_hash, to_vevent, uid_for
from itmosync.itmo.schedule import parse_schedule
from itmosync.models import Lesson
from tests.fakes import FakeCalendar

WINDOW_START = dt.date(2026, 9, 7)
WINDOW_END = dt.date(2026, 9, 13)


@pytest.fixture
def lessons(schedule_payload: dict[str, Any]) -> list[Lesson]:
    return parse_schedule(schedule_payload, DEFAULT_LESSON_TYPES)


def foreign_event(uid: str, when: dt.datetime) -> Event:
    event = Event()
    event.add("uid", uid)
    event.add("summary", "Стрижка")
    event.add("dtstart", when)
    event.add("dtend", when + dt.timedelta(hours=1))
    return event


def test_roundtrip_create_update_delete(lessons: list[Lesson], config: Config) -> None:
    backend = FakeCalendar()
    event = to_vevent(lessons[0], config)
    uid = uid_for(lessons[0])

    backend.create_event(event)
    assert backend.list_owned(WINDOW_START, WINDOW_END)[uid].content_hash == content_hash(
        lessons[0]
    )

    from dataclasses import replace

    moved = replace(lessons[0], room="9999")
    backend.update_event(uid, to_vevent(moved, config))
    assert backend.list_owned(WINDOW_START, WINDOW_END)[uid].content_hash == content_hash(moved)
    assert backend.sequences[uid] == 1

    backend.delete_event(uid)
    assert backend.list_owned(WINDOW_START, WINDOW_END) == {}


def test_foreign_events_are_invisible(lessons: list[Lesson], config: Config) -> None:
    backend = FakeCalendar()
    backend.create_event(to_vevent(lessons[0], config))
    backend.seed(foreign_event("personal-haircut@icloud.com", dt.datetime(2026, 9, 8, 12, 0)))

    owned = backend.list_owned(WINDOW_START, WINDOW_END)

    assert list(owned) == [uid_for(lessons[0])]


@pytest.mark.parametrize("operation", ["создать", "изменить", "удалить"])
def test_guard_refuses_foreign_uids(operation: str) -> None:
    with pytest.raises(CalendarError, match="чужим UID"):
        _guard_owned("personal-haircut@icloud.com", operation)


def test_guard_accepts_our_own_uid(lessons: list[Lesson]) -> None:
    _guard_owned(uid_for(lessons[0]), "удалить")


def test_backend_refuses_to_delete_a_foreign_event() -> None:
    backend = FakeCalendar()
    backend.seed(foreign_event("personal-haircut@icloud.com", dt.datetime(2026, 9, 8, 12, 0)))

    with pytest.raises(CalendarError, match="чужим UID"):
        backend.delete_event("personal-haircut@icloud.com")

    assert "personal-haircut@icloud.com" in backend.events


def test_events_outside_the_window_are_not_listed(lessons: list[Lesson], config: Config) -> None:
    backend = FakeCalendar()
    backend.create_event(to_vevent(lessons[0], config))

    assert backend.list_owned(dt.date(2026, 10, 1), dt.date(2026, 10, 7)) == {}


def test_icloud_parse_reads_hash_and_sequence(lessons: list[Lesson], config: Config) -> None:
    event = to_vevent(lessons[0], config)
    event.add("sequence", 4)

    class Item:
        data = event.to_ical().decode()

    parsed = ICloudCalendar._parse(Item())

    assert parsed == OwnedEvent(
        uid=uid_for(lessons[0]), content_hash=content_hash(lessons[0]), sequence=4, created=None
    )


def test_icloud_parse_skips_foreign_and_broken_items() -> None:
    class Foreign:
        data = foreign_event("x@icloud.com", dt.datetime(2026, 9, 8, 12, 0)).to_ical().decode()

    class Broken:
        data = "это не iCalendar"

    assert ICloudCalendar._parse(Foreign()) is None
    assert ICloudCalendar._parse(Broken()) is None
