# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

All eight steps of §14 of the spec are implemented and committed. Four features post-date the
spec: the token bookmarklet (`auth --bookmarklet`), macOS notifications (`report.notify`),
unattended runs through launchd (`schedule --install`) and the daily mode on top of it
(`--daily`, `sync --once-a-day`). The tree is green: `pytest`, `ruff` and `mypy --strict` pass.

Verified live:

- 2026-09-15, against the user's real iCloud calendar: `sync --dry-run` produced a correct plan
  and wrote nothing, a real `sync` applied it (87 events in the window, 5 created, 5 deleted),
  and the run right after it reported zero changes — idempotency holds on live data, not only in
  `test_idempotent_second_run`.
- Between 2026-09-28 and 2026-10-12 a launchd run hit a dead refresh token and logged the
  `TokenExpiredError` instructions, not a traceback.
- The daily agent, installed 2026-10-14, synced at install and then skipped hourly.
- 2026-10-17: the agent, silently broken for two days by the repo move, synced again from a
  standalone copy and was then reinstalled behind the shell wrapper (see Toolchain). Its
  fallback notification, triggered by hand with a failing stand-in program, showed up on screen.
- 2026-10-18: the Mac slept through the 00:00 firing and launchd ran it on wake at 00:01:56 —
  the replay the daily mode rests on. That sync went through the wrapper: 6 created, exit 0.

Still unverified from §15: `doctor` on a clean machine, the events showing up on the phone, a
hand-made event in the same calendar surviving three runs, and a deliberately revoked token.

`itmosync-tz.md` is the source of truth for scope, naming and architecture. Where it
disagrees with `docs/api-notes.md` about the API, **api-notes wins** and the spec gets
amended; several such corrections are already applied.

Three findings from reconnaissance that are easy to get wrong:

- The schedule request **must** send `Accept-Language: ru`. Without it the API answers `200`
  with the full structure but `subject`, `type` and `work_type` silently become `null`.
- The refresh token lives in the `auth._refresh_token.itmoId` cookie. `auth._token.itmoId` is
  the 30-minute *access* token, prefixed with `Bearer `.
- The lesson payload carries extra keys on some rows (`teacher_lesson`), so pydantic models
  must ignore unknown fields, not forbid them.

## What is being built

`itmosync` — a macOS CLI that pulls the user's personal class schedule from the internal
my.itmo API and mirrors it into a dedicated iCloud calendar over CalDAV. Run by hand or from a
launchd agent (daily on first wake, weekly or monthly), over a rolling 28-day window.

The whole project exists to be **idempotent**: a repeated run must produce zero changes, and a
changed lesson must *update* the existing event rather than delete-and-recreate it (recreation
shows up as "event deleted" on the user's phone and resets notification history).

## Toolchain

Python 3.12 managed by `uv`, pinned by `requires-python = ">=3.12,<3.13"`. The machine's own
`python3` and the `pytest` on `PATH` are 3.14 — never call them bare; go through `uv run`.

```bash
uv sync                              # install deps + the `dev` dependency group
uv run pytest                        # all tests
uv run pytest tests/test_sync_diff.py::test_idempotent_second_run   # a single test
uv run pytest --cov=itmosync.sync --cov=itmosync.ical.mapper --cov-report=term-missing
uv run ruff check src tests
uv run ruff format src tests
uv run mypy --strict src             # must be clean on all of src/
uv run itmosync <command>            # the CLI itself, straight from the working tree
```

`ruff` and `mypy --strict` passing on the entire `src/` is a hard acceptance criterion, not a
nice-to-have.

**Scope the coverage run to the two core modules, as above.** `fail_under = 90` in
`pyproject.toml` applies to whatever is being measured, while the 90% floor from the spec is
defined only for `sync.py` and `ical/mapper.py`. Measuring the whole package
(`--cov=src/itmosync`) therefore *fails* at ~70% even when nothing is wrong; the scoped run
reports ~99%.

Moving the repo leaves stale absolute shebangs in `.venv/bin`: `uv run mypy` then fails to
spawn, and `uv run pytest` silently falls through to the Python 3.14 pytest on `PATH` and dies
in collection. `uv sync --reinstall` fixes it.

The launchd agent runs a **standalone** install: `uv tool install .` puts `itmosync` in
`~/.local/bin` (a symlink into `~/.local/share/uv/tools/itmosync`), and
`uv tool install --reinstall .` refreshes it after a change — edits are live under `uv run`,
not in the installed copy. Never `--editable` for the agent: an editable install pins the
repo's absolute path, and when the repo moved from `~/Creat/Calendar` on 2026-10-15 every hourly
run died on `import itmosync` for two days without a single notification. `executable_path()`
therefore refuses an editable install (PEP 610 `direct_url.json`), which also catches
`schedule --install` run under `uv run`, where `shutil.which` finds `.venv/bin/itmosync` first.
The plist needs an absolute path regardless: launchd hands a job a minimal environment.

To see what the agent did: `~/Library/Logs/itmosync.log` (its stdout and stderr),
`itmosync schedule --status`, and `launchctl print gui/$(id -u)/ru.itmosync.sync` for the run
count and the last exit code. The plist is `~/Library/LaunchAgents/ru.itmosync.sync.plist`; it
runs `sync --notify`, plus `--once-a-day` in daily mode.

## The commands

```
itmosync init       write ~/.config/itmosync/config.toml (--force overwrites it)
itmosync auth       store secrets in the Keychain: exactly one of --set-token,
                    --set-apple-password, --bookmarklet; --from-clipboard goes with --set-token
itmosync doctor     config, ITMO ID, schedule API and iCloud, as a checklist
itmosync show       the schedule as a table (--week | --days N); touches no calendar at all
itmosync sync       the real thing: --dry-run, --days N, --force, --notify/--no-notify,
                    --once-a-day
itmosync schedule   the launchd agent: --install [--weekly] [--hour N] | --install --daily |
                    --status | --uninstall
```

`--config PATH` works for `init`, `doctor`, `show` and `sync`; `-v/--verbose` for `doctor`,
`show` and `sync`. `--daily` refuses `--weekly` and `--hour`.

`sync --dry-run` is also the answer to "what changed since last time?" — it reads my.itmo, reads
the calendar, diffs them and prints what it *would* do.

## Architecture

Data flows in one direction and each layer knows nothing about the next:

```
itmo/auth.py     refresh token (Keychain) -> access token (memory only); the rotated
                 refresh token goes back into the Keychain
itmo/client.py   the ONLY module containing my.itmo URLs; raw HTTP + the shared retry policy
itmo/schedule.py raw JSON -> validated pydantic -> list[Lesson]
models.py        Lesson (frozen dataclass), LessonType, normalize_subject
ical/mapper.py   Lesson -> VEVENT (pure; no network)
ical/icloud.py   the CalendarBackend protocol + the real CalDAV implementation
sync.py          the diff engine: desired vs existing -> SyncPlan -> SyncResult
report.py        SyncResult -> rich output, and macOS notifications through osascript
launchagent.py   the launchd plist and its shell wrapper: install / uninstall / status
cli.py           typer commands: parse args, call, print. No logic.
```

Beside that pipeline — the first three used by every layer, `runstamp.py` only by `cli.py`:

```
errors.py        the ItmosyncError hierarchy; each class carries its exit code as `exit_code`
config.py        pydantic models for config.toml, load_config / render_config
logging.py       setup_logging + SecretMaskingFilter, which masks secrets in every record
runstamp.py      the date of the last successful sync, which `--once-a-day` checks
```

Errors become exit codes in one place: `main()` in `cli.py` reads `exc.exit_code`. A new kind
of failure therefore means a new exception class in `errors.py`, not a literal code in a
command. The only literal codes are usage errors — `typer.Exit(code=2)` for a wrong combination
of flags or an empty Apple password — and `doctor`, which exits with the `exit_code` of its
first failed check.

`sync.py` must not know how the report is rendered — a future Telegram bot is explicitly out of
scope but must not require rewriting the engine.

`CalendarBackend` — a `Protocol` in `ical/icloud.py` — is the seam that keeps CalDAV out of the
test suite. `tests/fakes.py::FakeCalendar` implements it over a dict and imports the real
`_guard_owned`, so a test that reaches for a foreign UID fails exactly as production would.

### The two identity keys (the central design decision)

These are different keys and must never be conflated:

- **UID** — the stable anchor: `sha1("{date}|{normalized subject}|{label}|{index}")[:16]`, wrapped
  as `itmo-lesson-<16 hex>@itmosync`. Note **`label`** — the short string from `[lesson_types]`,
  not the `LessonType` enum: an unmapped type falls back to its first word rather than to
  `OTHER`, which is what keeps two unknown types on one day distinguishable. Time, room,
  building, teacher and links are deliberately **not** part of the UID, so a room change updates
  the event in place.
- **X-ITMO-HASH** — the content hash over start/end/subject/raw_type/room/building/teacher/
  online_url, stored as an X-property inside the VEVENT itself. It answers "does this event need
  updating?".

Because the hash lives in the event, **the calendar is the state store**. There is no SQLite and
none is needed in this version.

`Lesson.index` is the ordinal of a lesson among same (date, subject, label) lessons that day,
assigned after sorting by start time. Without it two lectures of one subject on one day collapse
into a single UID.

A consequence worth recognising in a report before someone calls it a bug: a lesson moved to
another weekday is a **new identity**, so the plan shows it as one delete plus one create rather
than an update. That is the design — the date is part of the UID. It happened for real on
2026-09-15, when a practice moved from Wednesday to Saturday and the plan read "5 deleted, 5
created". Only changes *within* the same day (time, room, building, teacher, link) are updates.

### Safety guards (all three are mandatory, all run before any write)

1. Empty `desired` while `existing` is non-empty aborts the run — protects against a broken API or
   a stale token returning `200` with an empty body wiping the calendar.
2. `len(to_delete) / max(len(existing), 1) > delete_threshold` (default 0.5) aborts and prints what
   would have been deleted.
3. Scope: reads, writes and deletes are confined to the window **and** to UIDs matching
   `^itmo-lesson-[0-9a-f]{16}@itmosync$`. Manually created events in the same calendar are invisible
   to the tool.

`sync --force` switches off guards 1 **and** 2 together (`check_guards` returns early); there is
no flag for just one of them. Guard 3 cannot be switched off.

## Invariants

- my.itmo URLs live **only** in `itmo/client.py`. Not one address string anywhere else. The
  exceptions are deliberate, not oversights. The `id.itmo.ru` token endpoint in `itmo/auth.py`
  stays where it is: ITMO ID is a separate Keycloak host, and the invariant is about my.itmo.
  And `my.itmo.ru` appears as text telling the user which page to open — in the
  `TokenExpiredError` instructions in `errors.py` and in the bookmarklet and its instructions in
  `cli.py` — never as an address the code requests.
- Import direction inside `itmo/`: `auth.py` imports `request_with_retries` from `client.py` at
  runtime, and `client.py` imports `Authenticator` only under `TYPE_CHECKING`. That is what keeps
  the cycle broken, so the shared retry policy stays in `client.py`.
- Keycloak **rotates the refresh token on every exchange**, and `Authenticator.refresh()` writes
  the new one back to the Keychain. A run is therefore what keeps the token alive — hence
  `schedule --install --daily` or `--weekly` over the monthly default, which would land right on
  the token's ~30-day lifetime. Every live command exchanges it: `doctor`, `show`, `sync`
  (`--dry-run` included) and `auth --set-token`. There is no locking, so two runs at once fight
  over the token and one of them fails with `TokenExpiredError`; the daily agent fires on the
  hour, so don't start a live command at :00. A my.itmo session in a browser competes for the
  same token too — see "Refresh exchange" in `docs/api-notes.md`.
- The daily agent is not a wake trigger — launchd has none. It fires every hour on the hour and
  at login, and relies on launchd replaying a firing missed during sleep on wake. Two pieces make
  that one sync a day: `runstamp.py` (`~/.local/state/itmosync/last-sync`, written by every
  successful non-dry `sync`, so a manual run counts too) and `sync --once-a-day`, which checks the
  stamp *before* touching the network. It then calls `wait_for_network()` outside the
  notify-on-failure block: no Wi-Fi right after wake is routine and the next hourly firing
  retries it, so `NetworkUnavailableError` must not notify. Tests never touch the real stamp —
  an autouse fixture in `conftest.py` redirects `runstamp.STAMP_PATH`.
- launchd runs itmosync through a `/bin/sh -c` wrapper (`launchagent.wrapper_script()`). It
  stays quiet on `QUIET_EXIT_CODES` — 0, and 3–6, after which itmosync has notified itself (4
  under `--once-a-day` may also be the deliberately silent no-network) — and notifies on its own
  for everything else: 1 (a crash, including a failed `import itmosync`), 2 (a config `sync`
  could not read before it knew whether to notify), 126/127 (the binary did not start). The
  tuple is built from the exception classes; a new exit code that itmosync reports itself
  belongs in it, or the user gets two notifications for one failure.
- The calendar package is named `ical`, never `calendar` — `calendar` shadows the stdlib module.
- Every deletion goes through **one** function that checks the UID mask. A second deletion path in
  the code is a bug.
- `apply_plan` writes in the order create → update → delete, so a lesson whose identity changed is
  never briefly absent from the calendar. `update_event` bumps `SEQUENCE` (without it clients do
  not notice the change) and carries `CREATED` over from the previous event.
- All datetimes are timezone-aware (`Europe/Moscow`, real `VTIMEZONE` written). Naive `datetime` is
  forbidden; floating times in VEVENTs are forbidden.
- Secrets live only in the macOS Keychain under service `itmosync` (`itmo_refresh_token`,
  `apple_app_password` — an Apple *app-specific* password). Never in the config file, logs, report
  output or error messages. The logger masks any token-looking string over 20 chars to `abcd…wxyz`;
  its regex deliberately excludes dots and slashes from the generic alternative, so paths and URLs
  survive masking and the logs stay debuggable.
- Config is `~/.config/itmosync/config.toml`, created by `itmosync init`; the `[lesson_types]`
  table maps the API's Russian type strings to short labels. An unknown type is *not* an error:
  fall back to the first word lowercased and log a warning containing the exact string.
- Test fixtures must never contain real names, ISU numbers or group numbers.
- `ScheduleSchemaError` must name the offending field and show the surrounding raw JSON — otherwise
  a changed upstream API is undebuggable.
- HTTP: 15 s timeout, 3 attempts (`MAX_ATTEMPTS`, i.e. two retries) with exponential backoff on
  5xx and network errors, none on 4xx. A schedule request answered with 401 exchanges a fresh
  access token and is repeated exactly once.
- Exit codes: `0` ok, including a `--once-a-day` run that skips because today is already synced ·
  `2` config, or an invalid combination of flags · `3` auth · `4` schedule API, including no
  network under `--once-a-day` (deliberately without a notification) · `5` calendar · `6` safety
  guard · `1` other, which in practice means a crash.

## Tests

The key test of the project is `test_idempotent_second_run`: two runs over identical data, the
second producing an empty plan. Nothing in the suite reaches the network, the Keychain or launchd:

- CalDAV: `tests/fakes.py::FakeCalendar`, an in-memory backend with the same interface.
- HTTP: `respx`, with `time.sleep` patched out of the retry loop.
- CLI: `test_cli_*.py` swap the names bound in `itmosync.cli` — `Authenticator`, `ItmoClient`,
  `SecretStore`, `ICloudCalendar`, `wait_for_network`, and `datetime`, which the `wired` fixture
  freezes to 2026-09-07, the week of `tests/fixtures/schedule_sample.json`. Keep `cli.py`
  importing these names directly, or the stubs stop reaching it.
- `runner.invoke(cli.app, …)` bypasses `main()`, so an `ItmosyncError` arrives as
  `result.exception`, not as its exit code.
- launchd: the plist is checked as data and `launchctl` is never called; the wrapper is run by a
  real `/bin/sh` against a stand-in program and a stand-in `osascript`.

## Language

- **Replies to the user are always in Russian**, whatever the language of the request or of the
  files involved.
- **The README is Russian.** It was deliberately rewritten from English at the user's request —
  it is the document people actually read to install and use the tool. Do not translate it back.
- **The rest of the technical documentation is English**: this file, everything under `docs/`,
  docstrings, code comments, identifiers and commit messages. (`itmosync-tz.md` is the user's own
  specification and stays Russian.)
- **Product text shipped to the end user is Russian** — CLI help, the report, log and error
  messages — and error messages say what to do next (`TokenExpiredError` prints the
  browser-console snippet for extracting a fresh refresh token).

## Plan before code

**No code is written before a plan is written and approved.** This applies to every task in this
repository, including ones that look small enough to just do.

The plan states what will be built, which files are created or changed, the order of the steps,
and how the result is verified. Present it and wait for the user's go-ahead; only then start
editing files. If the task turns out to differ from the plan while implementing it, stop and
re-agree the plan rather than improvising past it.

For work that maps onto §14 of the spec, a plan covers exactly one step of that list — the steps
are the natural commit-sized units.

## Commit discipline

Every §14 step got exactly one commit, and new work keeps that shape: one self-contained change
per commit, an English message, and the tree green (`ruff`, `mypy --strict`, `pytest`) before the
commit is made.

The first run against a live calendar is `--dry-run` only.
