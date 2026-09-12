# itmosync

A macOS command-line tool that mirrors your personal my.itmo class schedule into a
dedicated iCloud calendar. Run it by hand every week or so.

It is built to be idempotent: running it twice changes nothing the second time, and a
lesson that moves to another room **updates** its existing event instead of being deleted
and recreated — on a phone, a recreated event reads as a cancellation and loses its
notification history.

## Requirements

- macOS (secrets live in the Keychain)
- Python 3.12, managed by [uv](https://docs.astral.sh/uv/)
- An iCloud account and an ITMO ID login

## Install

```bash
git clone <this repo> && cd Calendar
uv sync
uv run itmosync --help
```

## First run

```bash
uv run itmosync init                       # writes ~/.config/itmosync/config.toml
uv run itmosync auth --set-token           # my.itmo refresh token
uv run itmosync auth --set-apple-password  # Apple ID app-specific password
uv run itmosync doctor                     # checks everything end to end
uv run itmosync sync --dry-run             # look before you write
uv run itmosync sync
```

Both secrets are read from stdin, never from a command-line argument, so they do not end
up in your shell history. They are stored in the macOS Keychain under the service
`itmosync` and never written to the config file, to logs, or to any error message.

### Getting the my.itmo refresh token

1. Open <https://my.itmo.ru> and sign in.
2. Open the browser console (⌥⌘I → Console) and run:

   ```js
   decodeURIComponent(document.cookie.split('auth._refresh_token.itmoId=')[1].split(';')[0])
   ```

3. Copy the string without the quotes and paste it into `itmosync auth --set-token`.

The token is good for about 30 days. Note the cookie name: `auth._token.itmoId` is the
30-minute **access** token and will not work — `itmosync` rejects it with an explanation
rather than failing later.

### Getting the Apple app-specific password

At <https://appleid.apple.com> → Sign-In and Security → App-Specific Passwords, create one
and paste it into `itmosync auth --set-apple-password`. It looks like `abcd-efgh-ijkl-mnop`.
This is **not** your main Apple ID password, which will not work here.

## Everyday use

```bash
uv run itmosync sync              # 28-day rolling window from the config
uv run itmosync sync --days 14    # a different horizon, once
uv run itmosync sync --dry-run    # print the plan, write nothing
uv run itmosync show --week       # print the schedule, never touch the calendar
```

A run prints what it did:

```
Синхронизация 12.09 – 09.10

  Добавлено    12
  Обновлено     3
  Удалено       2

  Обновлено:
    15.09  Матан · лек          Ауд. 1214 → Ауд. 2301
    17.09  Алгоритмы · пр       14:00–15:30 → 16:00–17:30

Готово за 6.2 с
```

## When the token expires

`itmosync` will stop with a message that prints the exact console snippet to run. Repeat the
refresh-token step above; nothing else needs to change.

The nominal lifetime is about 30 days, but it can end sooner: ITMO ID rotates the refresh
token on every exchange, so only one holder can be valid at a time. If your browser session
on my.itmo refreshes itself, the token stored for `itmosync` stops working, and vice versa.
That is not a bug to work around — just take a fresh token when it happens. For the same
reason, do not run two syncs at once.

## What it will not do

Three guards protect the calendar, and all of them run *before* anything is written:

- **Empty schedule.** If my.itmo returns no lessons while your calendar still has some,
  the run aborts. That is what a broken API or a stale token returning `200` with an empty
  body looks like, and it would otherwise wipe a month of events.
- **Mass deletion.** If more than `delete_threshold` (50% by default) of the events would
  be deleted, the run aborts and shows you the list.
- **Scope.** Reads, writes and deletes are limited to the sync window *and* to events
  `itmosync` itself created. An event you add by hand to the same calendar is invisible to
  the tool and survives any number of runs.

Both of the first two can be overridden with `--force` once you have looked at the list.

## Configuration

`~/.config/itmosync/config.toml`, created by `itmosync init`:

| Key | Default | Meaning |
|---|---|---|
| `calendar.name` | `ИТМО · Пары` | Calendar to use; created on first sync if absent |
| `calendar.timezone` | `Europe/Moscow` | Written into the events as a real VTIMEZONE |
| `sync.window_days` | `28` | How far ahead to sync |
| `sync.reminder_minutes` | `0` | `0` means no alarms at all |
| `sync.online_link_in_location` | `true` | Put the meeting link in the Location field, where it is tappable from a notification |
| `sync.delete_threshold` | `0.5` | Deletion share above which a run needs `--force` |
| `lesson_types` | see file | Maps the API's type strings to short labels |

An unknown lesson type is not an error: its first word is used and a warning names the
exact string so you can add it to `lesson_types`.

## Exit codes

`0` success · `2` configuration · `3` authentication · `4` schedule API · `5` calendar ·
`6` a safety guard fired · `1` anything else

## Development

```bash
uv run pytest                                   # full suite
uv run pytest tests/test_sync_diff.py::test_idempotent_second_run
uv run pytest --cov=itmosync.sync --cov=itmosync.ical.mapper --cov-report=term-missing
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy --strict src
```

`docs/api-notes.md` records the my.itmo API as it actually behaves and is the source of
truth for the parsing models. Tests never touch a live iCloud account: `tests/fakes.py`
provides an in-memory calendar with the same interface.
