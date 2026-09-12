"""Tests for parsing the my.itmo schedule payload."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from itmosync.config import DEFAULT_LESSON_TYPES
from itmosync.errors import ScheduleSchemaError
from itmosync.itmo.schedule import parse_schedule
from itmosync.models import LessonType


def parse(payload: dict[str, Any]) -> list[Any]:
    return parse_schedule(payload, DEFAULT_LESSON_TYPES)


def test_fixture_parses_completely(schedule_payload: dict[str, Any]) -> None:
    lessons = parse(schedule_payload)

    assert len(lessons) == 20
    assert {lesson.label for lesson in lessons} == {"лек", "пр", "лаб", "вст"}
    assert all(isinstance(lesson.date, dt.date) for lesson in lessons)
    assert all(lesson.start < lesson.end for lesson in lessons)


def test_lesson_fields_are_mapped(schedule_payload: dict[str, Any]) -> None:
    lessons = parse(schedule_payload)
    first = min(lessons, key=lambda item: (item.date, item.start))

    assert first.date == dt.date(2026, 9, 7)
    assert first.start == dt.time(8, 10)
    assert first.end == dt.time(9, 40)
    assert first.subject == "Алгоритмы и структуры данных"
    assert first.type is LessonType.LAB
    assert first.raw_type == "Лабораторные занятия"
    assert first.building == "ул.Ломоносова, д.9, лит. Б"
    assert first.room == "4213"
    assert first.note == "Романова Т.В."


def test_lesson_without_teacher_keeps_none(schedule_payload: dict[str, Any]) -> None:
    lessons = parse(schedule_payload)

    missing = [lesson for lesson in lessons if lesson.teacher is None]

    assert len(missing) == 1
    assert missing[0].subject == "Алгоритмы и структуры данных"


def test_online_lesson_is_detected_by_link(schedule_payload: dict[str, Any]) -> None:
    lessons = parse(schedule_payload)

    online = [lesson for lesson in lessons if lesson.is_online]

    assert len(online) == 1
    assert online[0].subject == "Архитектура вычислительных систем"
    assert online[0].online_url is not None
    # Almost every lesson is "Очно - дистанционный"; only the link makes one online.
    assert sum(1 for lesson in lessons if not lesson.is_online) == 19


def test_repeated_subject_gets_distinct_indexes(schedule_payload: dict[str, Any]) -> None:
    lessons = parse(schedule_payload)

    english = sorted(
        (
            lesson
            for lesson in lessons
            if lesson.date == dt.date(2026, 9, 7) and lesson.label == "пр"
        ),
        key=lambda item: item.start,
    )
    python_labs = sorted(
        (lesson for lesson in lessons if lesson.date == dt.date(2026, 9, 12)),
        key=lambda item: item.start,
    )

    assert [lesson.index for lesson in english] == [0, 1]
    assert [lesson.index for lesson in python_labs] == [0, 1]


def test_index_restarts_per_day(schedule_payload: dict[str, Any]) -> None:
    lessons = parse(schedule_payload)

    lectures = [lesson for lesson in lessons if lesson.label == "лек"]

    assert {lesson.index for lesson in lectures} == {0}


def test_unknown_type_falls_back_to_first_word(
    schedule_payload: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    schedule_payload["data"][0]["lessons"][0]["work_type"] = "Коллоквиум по вторникам"

    with caplog.at_level("WARNING", logger="itmosync"):
        lessons = parse(schedule_payload)

    odd = [lesson for lesson in lessons if lesson.label == "коллоквиум"]
    assert len(odd) == 1
    assert odd[0].type is LessonType.OTHER
    assert "Коллоквиум по вторникам" in caplog.text


def test_missing_subject_blames_accept_language(schedule_payload: dict[str, Any]) -> None:
    schedule_payload["data"][0]["lessons"][0]["subject"] = None

    with pytest.raises(ScheduleSchemaError) as excinfo:
        parse(schedule_payload)

    message = str(excinfo.value)
    assert "Accept-Language" in message
    assert "subject" in message
    assert "Фрагмент ответа" in message


def test_broken_time_is_reported_with_context(schedule_payload: dict[str, Any]) -> None:
    schedule_payload["data"][0]["lessons"][0]["time_start"] = "восемь утра"

    with pytest.raises(ScheduleSchemaError, match="ЧЧ:ММ"):
        parse(schedule_payload)


def test_empty_days_produce_no_lessons() -> None:
    payload = {"code": 0, "data": [{"date": "2026-09-13", "lessons": []}], "message": None}

    assert parse(payload) == []
