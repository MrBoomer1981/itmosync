"""Golden tests for Lesson → VEVENT."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from icalendar import Calendar, Event

from itmosync.config import DEFAULT_LESSON_TYPES, Config
from itmosync.ical.mapper import (
    content_hash,
    is_owned_uid,
    to_calendar,
    to_vevent,
    uid_for,
)
from itmosync.itmo.schedule import parse_schedule
from itmosync.models import Lesson, LessonType

SYNCED_AT = dt.datetime(2026, 9, 12, 10, 30, tzinfo=dt.UTC)


@pytest.fixture
def lessons(schedule_payload: dict[str, Any]) -> list[Lesson]:
    return parse_schedule(schedule_payload, DEFAULT_LESSON_TYPES)


def find(lessons: list[Lesson], subject_part: str, date: dt.date, label: str) -> Lesson:
    """Pick exactly one lesson: a subject can appear twice a day with different types."""
    matches = [
        lesson
        for lesson in lessons
        if subject_part in lesson.subject and lesson.date == date and lesson.label == label
    ]
    assert len(matches) == 1, f"ожидалось одно занятие, найдено {len(matches)}"
    return matches[0]


def value(event: Event, key: str) -> str:
    return str(event[key])


def test_uid_matches_the_owned_pattern(lessons: list[Lesson]) -> None:
    for lesson in lessons:
        assert is_owned_uid(uid_for(lesson))

    assert not is_owned_uid("39A4B1C2-0000@google.com")
    assert not is_owned_uid("itmo-lesson-nothex0123456789@itmosync")


def test_uid_ignores_room_time_and_teacher(lessons: list[Lesson]) -> None:
    lesson = lessons[0]
    from dataclasses import replace

    moved = replace(
        lesson,
        room="9999",
        building="Другой корпус",
        teacher="Кто-то Другой",
        start=dt.time(20, 0),
        end=dt.time(21, 30),
    )

    assert uid_for(moved) == uid_for(lesson)
    assert content_hash(moved) != content_hash(lesson)


def test_same_subject_twice_in_a_day_gets_two_uids(lessons: list[Lesson]) -> None:
    pair = sorted(
        (item for item in lessons if item.date == dt.date(2026, 9, 12)), key=lambda item: item.start
    )

    assert len(pair) == 2
    assert pair[0].subject == pair[1].subject
    assert uid_for(pair[0]) != uid_for(pair[1])


def test_golden_offline_lecture(lessons: list[Lesson], config: Config) -> None:
    lesson = find(lessons, "Дискретная математика", dt.date(2026, 9, 7), "лек")
    assert lesson.type is LessonType.LECTURE

    event = to_vevent(lesson, config, synced_at=SYNCED_AT)

    assert value(event, "summary") == "Дискретная математика · лек · 1124"
    assert value(event, "location") == "Ауд. 1124, ул.Ломоносова, д.9, лит. М"
    assert event.decoded("dtstart") == dt.datetime(2026, 9, 7, 11, 30, tzinfo=config.tz)
    assert event.decoded("dtend") == dt.datetime(2026, 9, 7, 13, 0, tzinfo=config.tz)
    assert "TZID=Europe/Moscow" in event.to_ical().decode()
    assert value(event, "status") == "CONFIRMED"
    assert value(event, "transp") == "OPAQUE"
    assert value(event, "X-ITMO-SOURCE") == "my.itmo"
    assert value(event, "X-ITMO-HASH") == content_hash(lesson)
    assert event["categories"].to_ical().decode() == "ИТМО"
    assert "url" not in event
    assert value(event, "description").splitlines() == [
        "Петрова М.С.",
        "Лекции",
        "X31234",
        "Синхронизировано itmosync, 12.09.2026 10:30",
    ]


def test_golden_online_lesson_puts_link_in_location(lessons: list[Lesson], config: Config) -> None:
    lesson = find(lessons, "Архитектура", dt.date(2026, 9, 9), "лек")
    assert lesson.is_online

    event = to_vevent(lesson, config, synced_at=SYNCED_AT)

    assert value(event, "summary") == "Архитектура вычислительных систем · лек · онлайн"
    assert value(event, "location") == lesson.online_url
    assert value(event, "url") == lesson.online_url


def test_link_stays_out_of_location_when_disabled(lessons: list[Lesson]) -> None:
    config = Config.model_validate(
        {"calendar": {"apple_id": "s@e.com"}, "sync": {"online_link_in_location": False}}
    )
    lesson = find(lessons, "Архитектура", dt.date(2026, 9, 9), "лек")

    event = to_vevent(lesson, config, synced_at=SYNCED_AT)

    assert value(event, "location") == "Ауд. 1404, Кронверкский пр., д.49, лит.А"
    assert value(event, "url") == lesson.online_url


def test_lesson_without_teacher_omits_the_line(lessons: list[Lesson], config: Config) -> None:
    lesson = find(lessons, "Алгоритмы", dt.date(2026, 9, 7), "лаб")
    assert lesson.teacher is None

    event = to_vevent(lesson, config, synced_at=SYNCED_AT)

    lines = value(event, "description").splitlines()
    assert lines[0] == "Лабораторные занятия"
    assert lines[-1] == "Синхронизировано itmosync, 12.09.2026 10:30"
    assert "Романова Т.В." in lines


def test_no_alarm_by_default(lessons: list[Lesson], config: Config) -> None:
    event = to_vevent(lessons[0], config, synced_at=SYNCED_AT)

    assert event.walk("VALARM") == []


def test_alarm_added_when_configured(lessons: list[Lesson]) -> None:
    config = Config.model_validate(
        {"calendar": {"apple_id": "s@e.com"}, "sync": {"reminder_minutes": 15}}
    )

    event = to_vevent(lessons[0], config, synced_at=SYNCED_AT)

    alarms = event.walk("VALARM")
    assert len(alarms) == 1
    assert alarms[0].decoded("trigger") == dt.timedelta(minutes=-15)


def test_calendar_carries_a_real_vtimezone(lessons: list[Lesson], config: Config) -> None:
    events = [to_vevent(lesson, config, synced_at=SYNCED_AT) for lesson in lessons[:3]]

    text = to_calendar(events, config).to_ical().decode()

    assert "BEGIN:VTIMEZONE" in text
    assert "TZID:Europe/Moscow" in text
    assert "TZOFFSETTO:+0300" in text
    assert text.count("BEGIN:VEVENT") == 3
    assert Calendar.from_ical(text).walk("VEVENT")


def test_hash_is_stable_across_identical_lessons(lessons: list[Lesson]) -> None:
    from dataclasses import replace

    lesson = lessons[0]
    twin = replace(lesson)

    assert content_hash(twin) == content_hash(lesson)
    assert uid_for(twin) == uid_for(lesson)


def test_summary_and_location_without_a_room(lessons: list[Lesson], config: Config) -> None:
    from dataclasses import replace

    lesson = replace(lessons[0], room=None)

    event = to_vevent(lesson, config, synced_at=SYNCED_AT)

    assert value(event, "summary") == "Алгоритмы и структуры данных · лаб"
    assert value(event, "location") == "ул.Ломоносова, д.9, лит. Б"


def test_location_is_omitted_when_nothing_is_known(lessons: list[Lesson], config: Config) -> None:
    from dataclasses import replace

    lesson = replace(lessons[0], room=None, building=None)

    event = to_vevent(lesson, config, synced_at=SYNCED_AT)

    assert "location" not in event
    assert value(event, "summary") == "Алгоритмы и структуры данных · лаб"
