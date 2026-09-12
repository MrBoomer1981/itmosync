"""Typer commands.

This module parses arguments, calls into the rest of the package and prints. It holds
no domain logic: if something here starts deciding what a lesson is, it belongs elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from itmosync.config import DEFAULT_CONFIG_PATH, load_config, render_config, write_config
from itmosync.errors import ConfigError, ItmosyncError
from itmosync.logging import setup_logging

app = typer.Typer(
    name="itmosync",
    help="Синхронизация расписания my.itmo с календарём iCloud.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

ConfigOption = Annotated[Path, typer.Option("--config", help="Путь к конфигу.")]


@dataclass(frozen=True, slots=True)
class Check:
    """One line of the `doctor` checklist."""

    name: str
    ok: bool
    detail: str = ""


def _print_checks(checks: list[Check]) -> bool:
    for check in checks:
        mark = "[green]✓[/green]" if check.ok else "[red]✗[/red]"
        detail = f" [dim]{check.detail}[/dim]" if check.detail else ""
        console.print(f"  {mark} {check.name}{detail}")
    return all(check.ok for check in checks)


@app.command(help="Создать конфиг с настройками по умолчанию.")
def init(
    config_path: ConfigOption = DEFAULT_CONFIG_PATH,
    force: Annotated[
        bool, typer.Option("--force", help="Перезаписать существующий конфиг.")
    ] = False,
) -> None:
    if config_path.exists() and not force:
        console.print(f"Конфиг уже существует: [bold]{config_path}[/bold]")
        console.print("Перезаписать — [bold]itmosync init --force[/bold]")
        raise typer.Exit(code=0)

    apple_id = typer.prompt("Apple ID (почта iCloud)")
    calendar_name = typer.prompt("Имя календаря", default="ИТМО · Пары")
    write_config(config_path, render_config(apple_id=apple_id, calendar_name=calendar_name))

    console.print(f"[green]✓[/green] Конфиг записан: [bold]{config_path}[/bold]")
    console.print("\nДальше:")
    console.print("  1. [bold]itmosync auth --set-token[/bold] — refresh-токен my.itmo")
    console.print(
        "  2. [bold]itmosync auth --set-apple-password[/bold] — пароль приложения Apple ID"
    )
    console.print("  3. [bold]itmosync doctor[/bold] — проверка всех подключений")


@app.command(help="Проверить конфиг, доступ к my.itmo и к календарю.")
def doctor(
    config_path: ConfigOption = DEFAULT_CONFIG_PATH,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Подробный лог.")] = False,
) -> None:
    setup_logging(verbose=verbose)
    checks: list[Check] = []

    try:
        config = load_config(config_path)
    except ConfigError:
        # The full message with its instructions is printed by main(); keep the line short here.
        checks.append(Check("Конфиг читается", ok=False))
        _print_checks(checks)
        raise
    checks.append(Check("Конфиг читается", ok=True, detail=str(config_path)))
    checks.append(Check("Таймзона корректна", ok=True, detail=config.calendar.timezone))

    if not _print_checks(checks):
        raise typer.Exit(code=1)


def main() -> None:
    """Console entry point: run the app and turn our errors into exit codes."""
    try:
        app()
    except ItmosyncError as exc:
        console.print(f"[bold red]✗[/bold red] {exc}")
        raise SystemExit(exc.exit_code) from exc
