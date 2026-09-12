"""Turn the raw my.itmo payload into Lesson objects.

The pydantic models below describe the response as it was actually recorded — see
docs/api-notes.md. They ignore unknown fields on purpose: the API adds keys on some rows
(`teacher_lesson`), and a strict model would fail on data that is perfectly usable.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from itmosync.errors import ScheduleSchemaError
from itmosync.logging import get_logger
from itmosync.models import Lesson, LessonType, normalize_subject

_log = get_logger()


class ApiLesson(BaseModel):
    model_config = ConfigDict(extra="ignore")

    subject: str
    work_type: str
    time_start: str
    time_end: str
    teacher_name: str | None = None
    room: str | None = None
    building: str | None = None
    group: str | None = None
    note: str | None = None
    zoom_url: str | None = None


class ApiDay(BaseModel):
    model_config = ConfigDict(extra="ignore")

    date: dt.date
    lessons: list[ApiLesson] = Field(default_factory=list)


class ApiResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: int = 0
    data: list[ApiDay] = Field(default_factory=list)
    message: str | None = None


def _excerpt(payload: Any, path: tuple[int | str, ...]) -> str:
    """Return the raw JSON around a failing field, so a changed API is debuggable."""
    node: Any = payload
    for step in path[:-1]:
        try:
            node = node[step]
        except (KeyError, IndexError, TypeError):
            break
    try:
        text = json.dumps(node, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        text = repr(node)
    return text if len(text) <= 800 else text[:800] + "\n…"


def _schema_error(exc: ValidationError, payload: Any) -> ScheduleSchemaError:
    error = exc.errors()[0]
    location = ".".join(str(part) for part in error["loc"]) or "<корень>"
    message = f"Ответ my.itmo не лёг в модель.\n\nПоле: {location}\nПричина: {error['msg']}"

    if error["loc"] and error["loc"][-1] in {"subject", "work_type"} and error.get("input") is None:
        message += (
            "\n\nПоле пришло пустым. Чаще всего это значит, что запрос ушёл без заголовка "
            "Accept-Language: ru — тогда my.itmo отдаёт 200, но обнуляет названия."
        )

    return ScheduleSchemaError(f"{message}\n\nФрагмент ответа:\n{_excerpt(payload, error['loc'])}")


def _classify(raw_type: str, lesson_types: Mapping[str, str]) -> tuple[LessonType, str]:
    """Map an API type string to a canonical type and the short label to display."""
    label = lesson_types.get(raw_type.strip())
    if label is None:
        label = raw_type.strip().split(" ")[0].casefold() or LessonType.OTHER.value
        _log.warning(
            "неизвестный тип занятия %r — использую %r; добавьте его в [lesson_types] конфига",
            raw_type,
            label,
        )
    try:
        return LessonType(label), label
    except ValueError:
        return LessonType.OTHER, label


def _time(value: str, field: str, payload: Any) -> dt.time:
    try:
        return dt.time.fromisoformat(value)
    except ValueError as exc:
        raise ScheduleSchemaError(
            f"Ответ my.itmo не лёг в модель.\n\nПоле: {field}\n"
            f"Причина: {value!r} — не время в формате ЧЧ:ММ\n\n"
            f"Фрагмент ответа:\n{_excerpt(payload, ())}"
        ) from exc


def parse_schedule(payload: dict[str, Any], lesson_types: Mapping[str, str]) -> list[Lesson]:
    """Validate the payload and flatten it into lessons, ordered by start time."""
    try:
        response = ApiResponse.model_validate(payload)
    except ValidationError as exc:
        raise _schema_error(exc, payload) from exc

    lessons: list[Lesson] = []
    for day in response.data:
        # The index disambiguates lessons that share (date, subject, label); it is only
        # meaningful once the day is ordered by start time.
        ordered = sorted(day.lessons, key=lambda item: item.time_start)
        seen: dict[tuple[str, str], int] = {}

        for item in ordered:
            lesson_type, label = _classify(item.work_type, lesson_types)
            key = (normalize_subject(item.subject), label)
            index = seen.get(key, 0)
            seen[key] = index + 1

            lessons.append(
                Lesson(
                    date=day.date,
                    start=_time(item.time_start, "time_start", payload),
                    end=_time(item.time_end, "time_end", payload),
                    subject=item.subject.strip(),
                    type=lesson_type,
                    label=label,
                    raw_type=item.work_type,
                    teacher=item.teacher_name or None,
                    room=item.room or None,
                    building=item.building or None,
                    online_url=item.zoom_url or None,
                    group=item.group or None,
                    note=item.note.strip() if item.note and item.note.strip() else None,
                    index=index,
                )
            )

    return lessons
