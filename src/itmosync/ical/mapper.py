"""Lesson → VEVENT.

Two different keys live here and must not be confused:

* the **UID** anchors an event across runs and changes as rarely as possible, so editing a
  room updates the event in place instead of deleting and recreating it;
* the **X-ITMO-HASH** covers the content and answers "does this event need updating?".

Because the hash travels inside the VEVENT, the calendar itself is the state store and no
local database is needed.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from typing import Final

from icalendar import Alarm, Calendar, Event, Timezone

from itmosync.config import Config
from itmosync.models import Lesson, normalize_subject

UID_SUFFIX: Final = "@itmosync"
UID_PREFIX: Final = "itmo-lesson-"
UID_PATTERN: Final = re.compile(rf"^{re.escape(UID_PREFIX)}[0-9a-f]{{16}}{re.escape(UID_SUFFIX)}$")

PRODID: Final = "-//itmosync//RU"
CATEGORY: Final = "ИТМО"
SOURCE: Final = "my.itmo"
HASH_PROPERTY: Final = "X-ITMO-HASH"
SOURCE_PROPERTY: Final = "X-ITMO-SOURCE"
# Kept so a sync report can say *what* changed; DESCRIPTION is prose and not parseable.
TEACHER_PROPERTY: Final = "X-ITMO-TEACHER"


def is_owned_uid(uid: str) -> bool:
    """True only for UIDs itmosync generated. The single gate on what we may touch."""
    return bool(UID_PATTERN.match(uid))


def uid_for(lesson: Lesson) -> str:
    """Stable event identity.

    Time, room, building, teacher and links are deliberately excluded: if they were part of
    the UID, a room change would delete one event and create another, which on a phone reads
    as "event deleted" and throws away the notification history.
    """
    key = (
        f"{lesson.date:%Y-%m-%d}|{normalize_subject(lesson.subject)}|{lesson.label}|{lesson.index}"
    )
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"{UID_PREFIX}{digest}{UID_SUFFIX}"


def content_hash(lesson: Lesson) -> str:
    """Hash of everything a change in which should update the event."""
    key = "|".join(
        [
            lesson.start.isoformat(),
            lesson.end.isoformat(),
            lesson.subject,
            lesson.raw_type,
            lesson.room or "",
            lesson.building or "",
            lesson.teacher or "",
            lesson.online_url or "",
        ]
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def summary_for(lesson: Lesson) -> str:
    parts = [lesson.subject, lesson.label]
    if lesson.is_online:
        parts.append("онлайн")
    elif lesson.room:
        parts.append(lesson.room)
    return " · ".join(parts)


def location_for(lesson: Lesson, *, online_link_in_location: bool) -> str:
    """Where the lesson happens, or the meeting link if that is more useful.

    A link in LOCATION is clickable straight from the notification and the widget, which is
    why it is the default.
    """
    if lesson.is_online and online_link_in_location and lesson.online_url:
        return lesson.online_url
    place = f"Ауд. {lesson.room}" if lesson.room else ""
    if lesson.building:
        place = f"{place}, {lesson.building}" if place else lesson.building
    return place


def description_for(lesson: Lesson, *, synced_at: dt.datetime) -> str:
    lines = [
        line
        for line in (
            lesson.teacher,
            lesson.raw_type,
            lesson.online_url,
            lesson.group,
            lesson.note,
        )
        if line
    ]
    lines.append(f"Синхронизировано itmosync, {synced_at:%d.%m.%Y %H:%M}")
    return "\n".join(lines)


def to_vevent(lesson: Lesson, config: Config, *, synced_at: dt.datetime | None = None) -> Event:
    """Build the VEVENT for one lesson."""
    tz = config.tz
    synced_at = synced_at or dt.datetime.now(tz)

    event = Event()
    event.add("uid", uid_for(lesson))
    event.add("summary", summary_for(lesson))
    event.add("dtstart", lesson.starts_at(tz))
    event.add("dtend", lesson.ends_at(tz))
    event.add("dtstamp", synced_at)
    event.add("description", description_for(lesson, synced_at=synced_at))
    event.add("categories", [CATEGORY])
    event.add("status", "CONFIRMED")
    event.add("transp", "OPAQUE")
    event.add(HASH_PROPERTY, content_hash(lesson))
    event.add(SOURCE_PROPERTY, SOURCE)
    if lesson.teacher:
        event.add(TEACHER_PROPERTY, lesson.teacher)

    location = location_for(lesson, online_link_in_location=config.sync.online_link_in_location)
    if location:
        event.add("location", location)
    if lesson.online_url:
        event.add("url", lesson.online_url)

    if config.sync.reminder_minutes > 0:
        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", summary_for(lesson))
        alarm.add("trigger", dt.timedelta(minutes=-config.sync.reminder_minutes))
        event.add_component(alarm)

    return event


def to_calendar(events: list[Event], config: Config) -> Calendar:
    """Wrap events in a VCALENDAR that carries a real VTIMEZONE.

    Floating times are not an option: without a VTIMEZONE the same event drifts when the
    phone travels, and iCloud will not resolve a bare TZID it was never given.
    """
    calendar = Calendar()
    calendar.add("prodid", PRODID)
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")

    horizon = dt.date.today()
    calendar.add_component(
        Timezone.from_tzid(
            config.calendar.timezone,
            first_date=horizon - dt.timedelta(days=365),
            last_date=horizon + dt.timedelta(days=730),
        )
    )
    for event in events:
        calendar.add_component(event)
    return calendar
