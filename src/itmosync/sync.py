"""The diff engine.

Compares what my.itmo says with what the calendar already holds, and applies the
difference. Two safety guards run before anything is written, and neither can be skipped
by accident: a run that would wipe the calendar has to be re-issued with --force.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from itmosync.config import Config
from itmosync.errors import SafetyGuardError
from itmosync.ical.icloud import CalendarBackend, OwnedEvent
from itmosync.ical.mapper import content_hash, location_for, to_vevent, uid_for
from itmosync.logging import get_logger
from itmosync.models import Lesson

_log = get_logger()


@dataclass(frozen=True, slots=True)
class Change:
    """An existing event whose content no longer matches the schedule."""

    uid: str
    lesson: Lesson
    before: OwnedEvent

    def describe(self, config: Config) -> str:
        """Say what actually changed, most user-visible difference first."""
        tz = config.tz
        new_start, new_end = self.lesson.starts_at(tz), self.lesson.ends_at(tz)
        if self.before.start is not None and (
            self.before.start != new_start or self.before.end != new_end
        ):
            old = f"{self.before.start:%H:%M}"
            if self.before.end is not None:
                old = f"{old}–{self.before.end:%H:%M}"
            return f"{old} → {new_start:%H:%M}–{new_end:%H:%M}"

        new_location = location_for(
            self.lesson, online_link_in_location=config.sync.online_link_in_location
        )
        if self.before.location and self.before.location != new_location:
            return f"{self.before.location} → {new_location}"

        new_teacher = self.lesson.teacher or ""
        if self.before.teacher != new_teacher:
            return "преподаватель изменён" if new_teacher else "преподаватель снят"

        return "изменены детали"


@dataclass(frozen=True, slots=True)
class SyncPlan:
    """What a sync would do. Building it writes nothing."""

    start: dt.date
    end: dt.date
    to_create: list[Lesson] = field(default_factory=list)
    to_update: list[Change] = field(default_factory=list)
    to_delete: list[OwnedEvent] = field(default_factory=list)
    existing_count: int = 0
    desired_count: int = 0

    @property
    def is_empty(self) -> bool:
        return not (self.to_create or self.to_update or self.to_delete)


@dataclass(frozen=True, slots=True)
class SyncResult:
    """What a sync actually did."""

    plan: SyncPlan
    created: int = 0
    updated: int = 0
    deleted: int = 0
    elapsed: float = 0.0
    dry_run: bool = False


def build_plan(
    lessons: list[Lesson],
    existing: dict[str, OwnedEvent],
    *,
    start: dt.date,
    end: dt.date,
) -> SyncPlan:
    """Diff the schedule against the calendar. Nothing outside the window is considered."""
    desired: dict[str, Lesson] = {}
    for lesson in lessons:
        if not (start <= lesson.date <= end):
            continue
        desired[uid_for(lesson)] = lesson

    to_create = [desired[uid] for uid in desired.keys() - existing.keys()]
    to_delete = [existing[uid] for uid in existing.keys() - desired.keys()]
    to_update = [
        Change(uid=uid, lesson=desired[uid], before=existing[uid])
        for uid in desired.keys() & existing.keys()
        if existing[uid].content_hash != content_hash(desired[uid])
    ]

    to_create.sort(key=lambda item: (item.date, item.start))
    to_update.sort(key=lambda item: (item.lesson.date, item.lesson.start))
    to_delete.sort(key=lambda item: (item.start or dt.datetime.min, item.summary))

    return SyncPlan(
        start=start,
        end=end,
        to_create=to_create,
        to_update=to_update,
        to_delete=to_delete,
        existing_count=len(existing),
        desired_count=len(desired),
    )


def check_guards(plan: SyncPlan, config: Config, *, force: bool = False) -> None:
    """Refuse to proceed when the plan looks like data loss rather than a change."""
    if force:
        _log.warning("предохранители отключены флагом --force")
        return

    if plan.desired_count == 0 and plan.existing_count > 0:
        raise SafetyGuardError(
            "API вернул пустое расписание, календарь не тронут.\n"
            f"В календаре {plan.existing_count} наших событий, в ответе my.itmo — ни одного.\n"
            "Так выглядит сломавшийся API или протухший токен, отдавший 200 с пустым телом.\n"
            "Если расписание действительно пусто (каникулы), повторите с --force."
        )

    share = len(plan.to_delete) / max(plan.existing_count, 1)
    if share > config.sync.delete_threshold:
        listing = "\n".join(
            f"  {event.start:%d.%m} {event.summary}" if event.start else f"  {event.summary}"
            for event in plan.to_delete[:20]
        )
        more = "" if len(plan.to_delete) <= 20 else f"\n  … ещё {len(plan.to_delete) - 20}"
        raise SafetyGuardError(
            f"Массовое удаление: {len(plan.to_delete)} из {plan.existing_count} событий "
            f"({share:.0%} — порог {config.sync.delete_threshold:.0%}). Календарь не тронут.\n\n"
            f"Собирались удалить:\n{listing}{more}\n\n"
            "Если это ожидаемо, повторите с --force."
        )


def apply_plan(
    plan: SyncPlan,
    backend: CalendarBackend,
    config: Config,
    *,
    dry_run: bool = False,
    elapsed: float = 0.0,
) -> SyncResult:
    """Write the plan. In dry-run mode the backend is never called."""
    if dry_run:
        return SyncResult(
            plan=plan,
            created=len(plan.to_create),
            updated=len(plan.to_update),
            deleted=len(plan.to_delete),
            elapsed=elapsed,
            dry_run=True,
        )

    synced_at = dt.datetime.now(config.tz)

    # Create before delete: if a lesson moved to a new identity, the new event exists
    # before the old one goes away, so the calendar is never briefly missing it.
    for lesson in plan.to_create:
        backend.create_event(to_vevent(lesson, config, synced_at=synced_at))
    for change in plan.to_update:
        backend.update_event(change.uid, to_vevent(change.lesson, config, synced_at=synced_at))
    for event in plan.to_delete:
        backend.delete_event(event.uid)

    return SyncResult(
        plan=plan,
        created=len(plan.to_create),
        updated=len(plan.to_update),
        deleted=len(plan.to_delete),
        elapsed=elapsed,
    )
