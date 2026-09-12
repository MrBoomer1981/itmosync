"""In-memory calendar backend.

Mirrors ICloudCalendar closely enough that the diff engine can be tested without a live
iCloud account — including the UID guard, so a test that tries to touch a foreign event
fails here exactly as it would in production.
"""

from __future__ import annotations

import datetime as dt

from icalendar import Event

from itmosync.errors import CalendarError
from itmosync.ical.icloud import OwnedEvent, _guard_owned
from itmosync.ical.mapper import HASH_PROPERTY, is_owned_uid


class FakeCalendar:
    """A dict pretending to be a calendar."""

    def __init__(self) -> None:
        self.events: dict[str, Event] = {}
        self.sequences: dict[str, int] = {}
        self.created: list[str] = []
        self.updated: list[str] = []
        self.deleted: list[str] = []

    @property
    def write_count(self) -> int:
        return len(self.created) + len(self.updated) + len(self.deleted)

    def seed(self, event: Event) -> None:
        """Put an event in the calendar without counting it as a write."""
        self.events[str(event["uid"])] = event

    def list_owned(self, start: dt.date, end: dt.date) -> dict[str, OwnedEvent]:
        owned: dict[str, OwnedEvent] = {}
        for uid, event in self.events.items():
            if not is_owned_uid(uid):
                continue
            when = event.decoded("dtstart")
            day = when.date() if isinstance(when, dt.datetime) else when
            if not (start <= day <= end):
                continue
            owned[uid] = OwnedEvent(
                uid=uid,
                content_hash=str(event.get(HASH_PROPERTY, "")),
                sequence=self.sequences.get(uid, 0),
            )
        return owned

    def create_event(self, event: Event) -> None:
        uid = str(event["uid"])
        _guard_owned(uid, "создать")
        self.events[uid] = event
        self.created.append(uid)

    def update_event(self, uid: str, event: Event) -> None:
        _guard_owned(uid, "изменить")
        if uid not in self.events:
            raise CalendarError(f"Событие {uid} не найдено.")
        self.sequences[uid] = self.sequences.get(uid, 0) + 1
        self.events[uid] = event
        self.updated.append(uid)

    def delete_event(self, uid: str) -> None:
        _guard_owned(uid, "удалить")
        if uid not in self.events:
            raise CalendarError(f"Событие {uid} не найдено.")
        del self.events[uid]
        self.deleted.append(uid)
