"""Rendering the run report.

Kept separate from sync.py so that the engine does not care where its result is shown —
a terminal today, something else later.
"""

from __future__ import annotations

from typing import Final

from rich.console import Console

from itmosync.config import Config
from itmosync.sync import SyncResult

MAX_LISTED: Final = 20


def render(result: SyncResult, config: Config, console: Console) -> None:
    """Print the report for a finished (or previewed) sync."""
    plan = result.plan

    if result.dry_run:
        console.print("[bold yellow]Предпросмотр, изменения не применены[/bold yellow]")
    console.print(f"[bold]Синхронизация {plan.start:%d.%m} – {plan.end:%d.%m}[/bold]\n")

    console.print(f"  Добавлено  {result.created:4}")
    console.print(f"  Обновлено  {result.updated:4}")
    console.print(f"  Удалено    {result.deleted:4}")

    if plan.to_update:
        console.print("\n  [bold]Обновлено:[/bold]")
        for change in plan.to_update[:MAX_LISTED]:
            lesson = change.lesson
            title = f"{lesson.subject} · {lesson.label}"
            console.print(f"    {lesson.date:%d.%m}  {title:<32.32} {change.describe(config)}")
        _print_more(console, len(plan.to_update))

    if plan.to_delete:
        console.print("\n  [bold]Удалено:[/bold]")
        for event in plan.to_delete[:MAX_LISTED]:
            when = f"{event.start:%d.%m}" if event.start else "??.??"
            span = ""
            if event.start is not None and event.end is not None:
                span = f"{event.start:%H:%M}–{event.end:%H:%M}"
            console.print(f"    {when}  {event.summary:<32.32} {span}")
        _print_more(console, len(plan.to_delete))

    if plan.is_empty:
        console.print("\n  Изменений нет.")

    console.print(f"\nГотово за {result.elapsed:.1f} с")


def _print_more(console: Console, total: int) -> None:
    if total > MAX_LISTED:
        console.print(f"    … ещё {total - MAX_LISTED}")
