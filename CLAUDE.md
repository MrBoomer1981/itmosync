# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

All eight steps of §14 of the spec are implemented and committed, and three features that
post-date the spec were added on top: the token bookmarklet (`auth --bookmarklet`), macOS
notifications (`report.notify`) and unattended runs through launchd (`schedule --install`).
The tool runs and the suite is green — 104 tests, `ruff` and `mypy --strict` clean.

The live acceptance pass from §15 is partly done. Verified against the user's real iCloud
calendar on 2026-09-15: `sync --dry-run` produced a correct plan and wrote nothing, a real
`sync` applied it (87 events in the window, 5 created, 5 deleted), and the run right after it
reported zero changes — idempotency holds on live data, not only in `test_idempotent_second_run`.

Still unverified from §15: `doctor` on a clean machine, the events showing up on the phone, a
hand-made event in the same calendar surviving three runs, and the message shown for a revoked
token.

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
weekly launchd agent, over a rolling 28-day window.

The whole project exists to be **idempotent**: a repeated run must produce zero changes, and a
changed lesson must *update* the existing event rather than delete-and-recreate it (recreation
shows up as "event deleted" on the user's phone and resets notification history).

## Toolchain

Python 3.12 managed by `uv`. The machine's default `python3` is 3.14 and 3.12 is not installed,
so pin it explicitly: `uv python install 3.12` and `requires-python = ">=3.12,<3.13"` in
`pyproject.toml`.

```bash
uv sync --all-extras                 # install deps + dev deps
uv run pytest                        # all tests
uv run pytest tests/test_sync_diff.py::test_idempotent_second_run   # a single test
uv run pytest --cov=itmosync.sync --cov=itmosync.ical.mapper --cov-report=term-missing
uv run ruff check src tests
uv run ruff format src tests
uv run mypy --strict src             # must be clean on all of src/
uv run itmosync <command>            # the CLI itself
```

`ruff` and `mypy --strict` passing on the entire `src/` is a hard acceptance criterion, not a
nice-to-have.

**Scope the coverage run to the two core modules, as above.** `fail_under = 90` in
`pyproject.toml` applies to whatever is being measured, while the 90% floor from the spec is
defined only for `sync.py` and `ical/mapper.py`. Measuring the whole package
(`--cov=src/itmosync`) therefore *fails* at ~68% even when nothing is wrong; the scoped run
reports ~99%.

For anything involving launchd, use the installed console script rather than `uv run`:
`uv tool install --editable .` puts `itmosync` in `~/.local/bin`. `launchagent.py` resolves the
program through `shutil.which("itmosync")`, and launchd hands a job a minimal environment in
which a bare command name would not resolve at all.

## The commands

```
itmosync init       write ~/.config/itmosync/config.toml
itmosync auth       store secrets in the Keychain (--set-token, --set-apple-password,
                    --bookmarklet, --from-clipboard)
itmosync doctor     config, ITMO ID, schedule API and iCloud, as a checklist
itmosync show       print the schedule as a table; touches no calendar at all
itmosync sync       the real thing; --dry-run prints the plan and writes nothing
itmosync schedule   the launchd agent: --install [--weekly] [--hour N], --status, --uninstall
```

`sync --dry-run` is also the answer to "what changed since last time?" — it reads my.itmo, reads
the calendar, diffs them and prints what it *would* do.

## Architecture

Data flows in one direction and each layer knows nothing about the next:

```
itmo/auth.py     refresh token -> access token (Keychain in, nothing out)
itmo/client.py   the ONLY module containing my.itmo URLs; raw HTTP + the shared retry policy
itmo/schedule.py raw JSON -> validated pydantic -> list[Lesson]
models.py        Lesson (frozen dataclass), LessonType, normalize_subject
ical/mapper.py   Lesson -> VEVENT (pure; no network)
ical/icloud.py   the CalendarBackend protocol + the real CalDAV implementation
sync.py          the diff engine: desired vs existing -> SyncPlan -> SyncResult
report.py        SyncResult -> rich output, and macOS notifications through osascript
launchagent.py   the launchd plist: install / uninstall / status
cli.py           typer commands: parse args, call, print. No logic.
```

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
   a stale token returning `200` with an empty body wiping the calendar. Only `--force` overrides.
2. `len(to_delete) / max(len(existing), 1) > delete_threshold` (default 0.5) aborts and prints what
   would have been deleted.
3. Scope: reads, writes and deletes are confined to the window **and** to UIDs matching
   `^itmo-lesson-[0-9a-f]{16}@itmosync$`. Manually created events in the same calendar are invisible
   to the tool.

## Invariants

- my.itmo URLs live **only** in `itmo/client.py`. Not one address string anywhere else. The
  `id.itmo.ru` constant in `itmo/auth.py` is a **deliberate exception**, not an oversight: the
  invariant is about my.itmo, and ITMO ID is a separate Keycloak host. Leave it where it is.
- Import direction inside `itmo/`: `auth.py` imports `request_with_retries` from `client.py` at
  runtime, and `client.py` imports `Authenticator` only under `TYPE_CHECKING`. That is what keeps
  the cycle broken, so the shared retry policy stays in `client.py`.
- Keycloak **rotates the refresh token on every exchange**, and `Authenticator.refresh()` writes
  the new one back to the Keychain. A run is therefore what keeps the token alive — hence
  `schedule --install --weekly` over the monthly default, which would land right on the token's
  ~30-day lifetime.
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
- HTTP: 15 s timeout, 3 retries with exponential backoff on 5xx and network errors, no retries on 4xx.
- Exit codes: `0` ok · `2` config · `3` auth · `4` schedule API · `5` calendar · `6` safety guard ·
  `1` other.

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

The key test of the project is `test_idempotent_second_run`: two runs over identical data, the
second producing an empty plan. CalDAV is never hit in tests — `tests/fakes.py` provides an
in-memory backend with the same interface.

The first run against a live calendar is `--dry-run` only.
