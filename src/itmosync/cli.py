"""Typer commands.

This module parses arguments, calls into the rest of the package and prints. It holds
no domain logic: if something here starts deciding what a lesson is, it belongs elsewhere.
"""

from __future__ import annotations

import getpass
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from itmosync.config import DEFAULT_CONFIG_PATH, load_config, render_config, write_config
from itmosync.errors import CalendarNotFoundError, ConfigError, ItmosyncError
from itmosync.ical.icloud import ICloudCalendar, OwnedEvent
from itmosync.itmo.auth import Authenticator, SecretStore, normalize_refresh_token
from itmosync.itmo.client import ItmoClient
from itmosync.itmo.schedule import parse_schedule
from itmosync.logging import setup_logging
from itmosync.report import render
from itmosync.sync import apply_plan, build_plan, check_guards

app = typer.Typer(
    name="itmosync",
    help="Синхронизация расписания my.itmo с календарём iCloud.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

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


def _read_clipboard() -> str:
    result = subprocess.run(["pbpaste"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ConfigError("Не удалось прочитать буфер обмена (pbpaste).")
    return result.stdout


def _clear_clipboard() -> None:
    subprocess.run(["pbcopy"], input="", text=True, check=False)


# Reads one cookie and copies it; it sends nothing anywhere. Kept on one line because a
# bookmarklet has to survive being pasted into a browser's URL field.
BOOKMARKLET = (
    "javascript:(function(){"
    "var m=document.cookie.match(/auth\\._refresh_token\\.itmoId=([^;]*)/);"
    "if(!m){alert('Токен не найден. Откройте my.itmo.ru и войдите в личный кабинет.');return;}"
    "navigator.clipboard.writeText(decodeURIComponent(m[1])).then(function(){"
    "alert('Токен скопирован.\\n\\nТеперь в терминале:\\n"
    "itmosync auth --set-token --from-clipboard');"
    "}).catch(function(){alert('Браузер не дал доступ к буферу обмена.');});"
    "})()"
)


def _print_bookmarklet() -> None:
    console.print("[bold]Закладка для получения токена[/bold]\n")
    console.print("1. Создайте в браузере новую закладку (⌘D, затем «Изменить»)")
    console.print("2. Назовите её, например, «Токен ИТМО»")
    console.print("3. В поле адреса вставьте строку ниже целиком:\n")
    console.print(BOOKMARKLET, style="cyan", soft_wrap=True)
    console.print("\n4. Откройте my.itmo.ru, войдите и нажмите закладку")
    console.print("5. Выполните: [bold]itmosync auth --set-token --from-clipboard[/bold]")
    console.print(
        "\n[dim]Закладка читает одну cookie и кладёт её в буфер обмена. "
        "Никуда ничего не отправляет.[/dim]"
    )


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
    bookmarklet: Annotated[
        bool,
        typer.Option("--bookmarklet", help="Показать закладку, копирующую токен в буфер обмена."),
    ] = False,
    from_clipboard: Annotated[
        bool,
        typer.Option("--from-clipboard", help="Взять секрет из буфера обмена, а не из stdin."),
    ] = False,
) -> None:
    if sum((set_token, set_apple_password, bookmarklet)) != 1:
        console.print(
            "Укажите ровно один флаг: --set-token, --set-apple-password или --bookmarklet"
        )
        raise typer.Exit(code=2)

    if bookmarklet:
        _print_bookmarklet()
        return

    store = SecretStore()

    if set_token:
        raw = _read_clipboard() if from_clipboard else _read_secret("Refresh-токен my.itmo: ")
        token = normalize_refresh_token(raw)
        store.set_refresh_token(token)
        console.print("[green]✓[/green] Токен сохранён в связке ключей, проверяю обмен…")
        with Authenticator(store) as authenticator:
            authenticator.refresh()
        console.print("[green]✓[/green] ITMO ID принял токен")
        if from_clipboard:
            # The token is a live credential; leaving it in the clipboard for the next
            # accidental ⌘V is a needless risk.
            _clear_clipboard()
            console.print("[dim]Буфер обмена очищен.[/dim]")
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

        def fetch_schedule() -> str:
            today = datetime.now(config.tz).date() if config else date.today()
            with Authenticator() as authenticator, ItmoClient(authenticator) as client:
                payload = client.fetch_schedule(today, today + timedelta(days=6))
            assert config is not None
            return f"занятий на неделю: {len(parse_schedule(payload, config.lesson_types))}"

        def reach_icloud() -> str:
            assert config is not None
            with ICloudCalendar(config, SecretStore().get_apple_password()) as calendar:
                calendar.connect()
            return config.calendar.apple_id

        def find_calendar() -> str:
            assert config is not None
            with ICloudCalendar(config, SecretStore().get_apple_password()) as calendar:
                try:
                    calendar.open_calendar(create=False)
                except CalendarNotFoundError:
                    # A clean machine has no calendar yet; the first sync creates it.
                    return f"«{config.calendar.name}» пока нет, создам при первом sync"
            return f"«{config.calendar.name}» найден"

        checks.append(_check("Refresh-токен обменивается", exchange_token))
        checks.append(_check("API расписания отвечает", fetch_schedule))
        checks.append(_check("iCloud доступен", reach_icloud))
        checks.append(_check("Календарь на месте", find_calendar))

    raise typer.Exit(code=_print_checks(checks))


@app.command(help="Показать расписание таблицей, в календарь ничего не пишет.")
def show(
    config_path: ConfigOption = DEFAULT_CONFIG_PATH,
    days: Annotated[
        int | None, typer.Option("--days", help="Сколько дней показать, начиная с сегодня.")
    ] = None,
    week: Annotated[bool, typer.Option("--week", help="Ближайшая неделя (7 дней).")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Подробный лог.")] = False,
) -> None:
    setup_logging(verbose=verbose)
    config = load_config(config_path)

    span = 7 if week else (days if days is not None else config.sync.window_days)
    start = datetime.now(config.tz).date()
    end = start + timedelta(days=span - 1)

    with Authenticator() as authenticator, ItmoClient(authenticator) as client:
        payload = client.fetch_schedule(start, end)
    lessons = parse_schedule(payload, config.lesson_types)

    if not lessons:
        console.print(f"Занятий с {start:%d.%m} по {end:%d.%m} нет.")
        return

    table = Table(title=f"Расписание {start:%d.%m} – {end:%d.%m}", title_style="bold")
    table.add_column("Дата")
    table.add_column("Время")
    table.add_column("Предмет")
    table.add_column("Тип")
    table.add_column("Место")
    table.add_column("Преподаватель")

    previous: date | None = None
    for lesson in sorted(lessons, key=lambda item: (item.date, item.start)):
        if previous is not None and lesson.date != previous:
            table.add_section()
        previous = lesson.date
        place = (
            "онлайн"
            if lesson.is_online
            else ", ".join(part for part in (lesson.room, lesson.building) if part)
        )
        table.add_row(
            f"{lesson.date:%d.%m} {_WEEKDAYS[lesson.date.weekday()]}",
            f"{lesson.start:%H:%M}–{lesson.end:%H:%M}",
            lesson.subject,
            lesson.label,
            place,
            lesson.teacher or "",
        )

    console.print(table)
    console.print(f"Всего занятий: [bold]{len(lessons)}[/bold]")


@app.command(help="Синхронизировать расписание с календарём iCloud.")
def sync(
    config_path: ConfigOption = DEFAULT_CONFIG_PATH,
    days: Annotated[
        int | None, typer.Option("--days", help="Горизонт в днях вместо значения из конфига.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Показать план, ничего не записывая.")
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Отключить предохранители.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Подробный лог.")] = False,
) -> None:
    setup_logging(verbose=verbose)
    config = load_config(config_path)

    span = days if days is not None else config.sync.window_days
    start = datetime.now(config.tz).date()
    end = start + timedelta(days=span - 1)

    started = time.perf_counter()
    with Authenticator() as authenticator, ItmoClient(authenticator) as client:
        payload = client.fetch_schedule(start, end)
    lessons = parse_schedule(payload, config.lesson_types)

    with ICloudCalendar(config, SecretStore().get_apple_password()) as calendar:
        calendar.connect()
        existing: dict[str, OwnedEvent] = {}
        try:
            # A dry run must not create anything, not even the calendar — and the very first
            # run is supposed to be a dry run, so a missing calendar is expected here.
            calendar.open_calendar(create=not dry_run)
        except CalendarNotFoundError:
            if not dry_run:
                raise
            console.print(
                f"[yellow]Календаря «{config.calendar.name}» ещё нет — "
                "он будет создан при реальном запуске.[/yellow]\n"
            )
        else:
            existing = calendar.list_owned(start, end)

        plan = build_plan(lessons, existing, start=start, end=end)
        check_guards(plan, config, force=force)
        result = apply_plan(
            plan,
            calendar,
            config,
            dry_run=dry_run,
            elapsed=time.perf_counter() - started,
        )

    render(result, config, console)


def main() -> None:
    """Console entry point: run the app and turn our errors into exit codes."""
    try:
        app()
    except ItmosyncError as exc:
        console.print(f"[bold red]✗[/bold red] {exc}")
        raise SystemExit(exc.exit_code) from exc
