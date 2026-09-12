"""Domain model.

A Lesson is one occurrence of one class on one day — my.itmo has no notion of a
recurring series, and neither do we.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

_WHITESPACE: Final = re.compile(r"\s+")
_TRAILING_PUNCTUATION: Final = re.compile(r"[\s.,;:!?]+$")
_QUOTES: Final = str.maketrans("", "", "\"'«»„“”`")


class LessonType(StrEnum):
    """Canonical lesson types.

    A lesson also carries a free-form `label` (see Lesson): the config maps the API's type
    string to a short label, and an unmapped type falls back to its first word rather than
    to OTHER. The label, not this enum, is what titles and UIDs use, so two unknown types
    on the same day stay distinguishable.
    """

    LECTURE = "лек"
    PRACTICE = "пр"
    LAB = "лаб"
    MEETING = "вст"
    CONSULT = "конс"
    EXAM = "экз"
    OTHER = "зан"


def normalize_subject(subject: str) -> str:
    """Fold a subject name to its identity form.

    Used by the UID and by the index, so that a renamed-but-equivalent subject
    ("Матанализ." vs "матанализ") keeps pointing at the same event.
    """
    folded = subject.translate(_QUOTES).casefold()
    folded = _WHITESPACE.sub(" ", folded).strip()
    return _TRAILING_PUNCTUATION.sub("", folded)


@dataclass(frozen=True, slots=True)
class Lesson:
    """One class occurrence as itmosync understands it."""

    date: dt.date
    start: dt.time
    end: dt.time
    subject: str
    type: LessonType
    label: str
    raw_type: str
    teacher: str | None = None
    room: str | None = None
    building: str | None = None
    online_url: str | None = None
    group: str | None = None
    note: str | None = None
    index: int = 0

    @property
    def is_online(self) -> bool:
        """Online means there is a meeting link, not what `format` says.

        `format_id = 2` ("Очно - дистанционный") is set on almost every lesson regardless
        of whether a link exists — see docs/api-notes.md.
        """
        return bool(self.online_url)

    def starts_at(self, tz: dt.tzinfo) -> dt.datetime:
        return dt.datetime.combine(self.date, self.start, tzinfo=tz)

    def ends_at(self, tz: dt.tzinfo) -> dt.datetime:
        return dt.datetime.combine(self.date, self.end, tzinfo=tz)
