"""iCloud calendar access over CalDAV.

Everything here is scoped twice: to the sync window, and to UIDs itmosync generated.
Events a person created by hand in the same calendar are never read, changed or deleted.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Protocol

# caldav re-exports lazily through __getattr__, which mypy cannot follow; import the
# implementation modules directly so the types survive.
from caldav.davclient import DAVClient
from caldav.lib import error as caldav_error
from icalendar import Calendar as ICalendar
from icalendar import Event

from itmosync.config import Config
from itmosync.errors import CalDavAuthError, CalendarError, CalendarNotFoundError
from itmosync.ical.mapper import HASH_PROPERTY, is_owned_uid, to_calendar
from itmosync.logging import get_logger

_log = get_logger()


@dataclass(frozen=True, slots=True)
class OwnedEvent:
    """An event in the calendar that itmosync created."""

    uid: str
    content_hash: str
    sequence: int = 0
    created: dt.datetime | None = None


class CalendarBackend(Protocol):
    """What sync.py needs from a calendar.

    Kept narrow on purpose: the tests substitute an in-memory implementation, so the diff
    engine can be exercised without ever touching a live iCloud account.
    """

    def list_owned(self, start: dt.date, end: dt.date) -> dict[str, OwnedEvent]: ...

    def create_event(self, event: Event) -> None: ...

    def update_event(self, uid: str, event: Event) -> None: ...

    def delete_event(self, uid: str) -> None: ...


def _guard_owned(uid: str, operation: str) -> None:
    """The single gate every write and delete passes through."""
    if not is_owned_uid(uid):
        raise CalendarError(
            f"Отказ: попытка {operation} события с чужим UID {uid!r}. "
            "itmosync трогает только собственные события."
        )


class ICloudCalendar:
    """The real CalDAV backend."""

    def __init__(self, config: Config, password: str) -> None:
        self._config = config
        self._password = password
        self._client: Any = None
        self._calendar: Any = None
        self._hrefs: dict[str, Any] = {}

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> ICloudCalendar:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connect(self) -> None:
        """Open the CalDAV session and check the credentials."""
        self._client = DAVClient(
            url=self._config.calendar.caldav_url,
            username=self._config.calendar.apple_id,
            password=self._password,
        )
        try:
            self._client.principal()
        except caldav_error.AuthorizationError as exc:
            raise CalDavAuthError(
                "iCloud отклонил Apple ID или пароль приложения.\n"
                "Проверьте, что используется пароль приложения с appleid.apple.com, "
                "а не основной пароль: itmosync auth --set-apple-password"
            ) from exc
        except Exception as exc:  # caldav raises a wide range of transport errors
            raise CalendarError(f"Не удалось подключиться к iCloud: {exc}") from exc

    def _principal(self) -> Any:
        if self._client is None:
            self.connect()
        return self._client.principal()

    def open_calendar(self, *, create: bool = True) -> None:
        """Find the configured calendar, creating it when allowed."""
        name = self._config.calendar.name
        principal = self._principal()
        try:
            for calendar in principal.calendars():
                if str(calendar.name) == name:
                    self._calendar = calendar
                    return
        except Exception as exc:  # transport and parsing errors both surface here
            raise CalendarError(f"Не удалось получить список календарей iCloud: {exc}") from exc

        if not create:
            raise CalendarNotFoundError(f"Календарь «{name}» не найден в iCloud.")

        try:
            self._calendar = principal.make_calendar(name=name)
        except Exception as exc:
            raise CalendarNotFoundError(
                f"Календарь «{name}» не найден, и создать его не удалось: {exc}"
            ) from exc
        _log.info("создан календарь «%s»", name)

    @property
    def calendar(self) -> Any:
        if self._calendar is None:
            self.open_calendar()
        return self._calendar

    def list_owned(self, start: dt.date, end: dt.date) -> dict[str, OwnedEvent]:
        """Read our events in the window. Foreign events are skipped, not just ignored."""
        tz = self._config.tz
        window_start = dt.datetime.combine(start, dt.time.min, tzinfo=tz)
        window_end = dt.datetime.combine(end, dt.time.max, tzinfo=tz)

        try:
            found = self.calendar.search(
                start=window_start, end=window_end, event=True, expand=False
            )
        except Exception as exc:
            raise CalendarError(f"Не удалось прочитать события из iCloud: {exc}") from exc

        owned: dict[str, OwnedEvent] = {}
        self._hrefs.clear()
        for item in found:
            parsed = self._parse(item)
            if parsed is None:
                continue
            owned[parsed.uid] = parsed
            self._hrefs[parsed.uid] = item
        return owned

    @staticmethod
    def _parse(item: Any) -> OwnedEvent | None:
        try:
            calendar = ICalendar.from_ical(item.data)
        except (ValueError, KeyError):
            return None
        for component in calendar.walk("VEVENT"):
            uid = str(component.get("uid", ""))
            if not is_owned_uid(uid):
                return None
            sequence = component.get("sequence")
            created = component.get("created")
            return OwnedEvent(
                uid=uid,
                content_hash=str(component.get(HASH_PROPERTY, "")),
                sequence=int(sequence) if sequence is not None else 0,
                created=created.dt if created is not None else None,
            )
        return None

    def _serialize(self, event: Event) -> str:
        return str(to_calendar([event], self._config).to_ical().decode("utf-8"))

    def create_event(self, event: Event) -> None:
        uid = str(event["uid"])
        _guard_owned(uid, "создать")
        try:
            self.calendar.save_event(self._serialize(event))
        except Exception as exc:
            raise CalendarError(f"Не удалось создать событие {uid}: {exc}") from exc

    def update_event(self, uid: str, event: Event) -> None:
        _guard_owned(uid, "изменить")
        existing = self._hrefs.get(uid)
        if existing is None:
            raise CalendarError(f"Событие {uid} не найдено в прочитанном окне.")

        previous = self._parse(existing)
        # Bumping SEQUENCE is what makes clients notice the change; keeping CREATED keeps
        # the event's own history intact.
        event["sequence"] = (previous.sequence if previous else 0) + 1
        if previous is not None and previous.created is not None:
            event.add("created", previous.created)

        try:
            existing.data = self._serialize(event)
            existing.save()
        except Exception as exc:
            raise CalendarError(f"Не удалось обновить событие {uid}: {exc}") from exc

    def delete_event(self, uid: str) -> None:
        """The only path to deletion in the codebase."""
        _guard_owned(uid, "удалить")
        existing = self._hrefs.get(uid)
        if existing is None:
            raise CalendarError(f"Событие {uid} не найдено в прочитанном окне.")
        try:
            existing.delete()
        except Exception as exc:
            raise CalendarError(f"Не удалось удалить событие {uid}: {exc}") from exc
        self._hrefs.pop(uid, None)
