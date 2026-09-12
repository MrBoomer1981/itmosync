"""Typer commands.

This module parses arguments, calls into the rest of the package and prints. It holds
no domain logic: if something here starts deciding what a lesson is, it belongs elsewhere.
"""

from __future__ import annotations

import getpass
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from itmosync.config import DEFAULT_CONFIG_PATH, load_config, render_config, write_config
from itmosync.errors import ItmosyncError
from itmosync.itmo.auth import Authenticator, SecretStore, normalize_refresh_token
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
    hint: str = ""
    exit_code: int = 1


def _print_checks(checks: list[Check]) -> int:
    """Print the checklist and return the exit code of the first failure, or 0."""
    for check in checks:
        mark = "[green]✓[/green]" if check.ok else "[red]✗[/red]"
        detail = f" [dim]{check.detail}[/dim]" if check.detail else ""
        console.print(f"  {mark} {check.name}{detail}")

    failed = [check for check in checks if not check.ok]
    for check in failed:
        if check.hint:
            console.print(f"\n[bold]{check.name}[/bold]\n{check.hint}")
    return failed[0].exit_code if failed else 0


def _check(name: str, action: Callable[[], str]) -> Check:
    """Run one doctor probe, turning our exceptions into a checklist line."""
    try:
        detail = action()
    except ItmosyncError as exc:
        first_line = str(exc).splitlines()[0]
        return Check(name, ok=False, detail=first_line, hint=str(exc), exit_code=exc.exit_code)
    return Check(name, ok=True, detail=detail)


def _read_secret(prompt: str) -> str:
    """Read a secret from stdin: hidden when interactive, piped otherwise.

    Never accept a secret as a command-line argument — it would land in shell history.
    """
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    return sys.stdin.read()


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


@app.command(help="Записать секреты в связку ключей macOS.")
def auth(
    set_token: Annotated[
        bool, typer.Option("--set-token", help="Refresh-токен my.itmo (ввод из stdin).")
    ] = False,
    set_apple_password: Annotated[
        bool,
        typer.Option("--set-apple-password", help="Пароль приложения Apple ID (ввод из stdin)."),
    ] = False,
) -> None:
    if set_token == set_apple_password:
        console.print("Укажите ровно один флаг: --set-token или --set-apple-password")
        raise typer.Exit(code=2)

    store = SecretStore()

    if set_token:
        token = normalize_refresh_token(_read_secret("Refresh-токен my.itmo: "))
        store.set_refresh_token(token)
        console.print("[green]✓[/green] Токен сохранён в связке ключей, проверяю обмен…")
        with Authenticator(store) as authenticator:
            authenticator.refresh()
        console.print("[green]✓[/green] ITMO ID принял токен")
        return

    password = _read_secret("Пароль приложения Apple ID: ").strip()
    if not password:
        console.print("Пустой пароль не сохранён")
        raise typer.Exit(code=2)
    store.set_apple_password(password)
    console.print("[green]✓[/green] Пароль приложения сохранён в связке ключей")


@app.command(help="Проверить конфиг, доступ к my.itmo и к календарю.")
def doctor(
    config_path: ConfigOption = DEFAULT_CONFIG_PATH,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Подробный лог.")] = False,
) -> None:
    setup_logging(verbose=verbose)
    checks: list[Check] = []

    config = None

    def read_config() -> str:
        nonlocal config
        config = load_config(config_path)
        return str(config_path)

    checks.append(_check("Конфиг читается", read_config))

    if config is not None:
        checks.append(Check("Таймзона корректна", ok=True, detail=config.calendar.timezone))

        def exchange_token() -> str:
            with Authenticator() as authenticator:
                token = authenticator.refresh()
            left = token.expires_at - datetime.now(UTC)
            return f"access действителен ещё {int(left.total_seconds() // 60)} мин"

        checks.append(_check("Refresh-токен обменивается", exchange_token))

    raise typer.Exit(code=_print_checks(checks))


def main() -> None:
    """Console entry point: run the app and turn our errors into exit codes."""
    try:
        app()
    except ItmosyncError as exc:
        console.print(f"[bold red]✗[/bold red] {exc}")
        raise SystemExit(exc.exit_code) from exc
