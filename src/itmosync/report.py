"""Rendering the run report.

Kept separate from sync.py so that the engine does not care where its result is shown —
a terminal today, something else later.
"""

from __future__ import annotations

import subprocess
from typing import Final

from rich.console import Console

from itmosync.config import Config
from itmosync.logging import get_logger
from itmosync.sync import SyncResult

_log = get_logger()

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


def _display_notification(title: str, message: str) -> None:
    """Post a macOS notification.

    Uses osascript rather than a library: it needs no dependency and it works from a
    launchd agent, where there is no terminal to print to.
    """
    script = (
        f"display notification {_applescript_string(message)} "
        f"with title {_applescript_string(title)}"
    )
    try:
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        # A missing notification must never fail a sync that already succeeded.
        _log.debug("не удалось показать уведомление: %s", exc)


def _applescript_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def notify(result: SyncResult, config: Config) -> None:
    """Tell the user what the run did, if notifications are switched on."""
    if not config.sync.notify:
        return
    prefix = "Предпросмотр: " if result.dry_run else ""
    _display_notification(
        "itmosync",
        f"{prefix}добавлено {result.created}, обновлено {result.updated}, удалено {result.deleted}",
    )


def notify_failure(error: Exception, config: Config) -> None:
    """Report a failed run.

    Without this an automated run fails in silence, which is the whole problem with
    automating something nobody watches.
    """
    if not config.sync.notify:
        return
    _display_notification("itmosync — ошибка", str(error).splitlines()[0])
