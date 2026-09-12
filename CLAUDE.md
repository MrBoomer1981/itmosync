# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

All eight steps of the spec are implemented and committed; the tool runs. What remains is
the live acceptance pass from §15 — a real `sync` against the user's iCloud account.

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
my.itmo API and mirrors it into a dedicated iCloud calendar over CalDAV. Run manually, roughly
weekly, over a rolling 28-day window.

The whole project exists to be **idempotent**: a repeated run must produce zero changes, and a
changed lesson must *update* the existing event rather than delete-and-recreate it (recreation
shows up as "event deleted" on the user's phone and resets notification history).

## Toolchain

Python 3.12 managed by `uv`. The machine's default `python3` is 3.14 and 3.12 is not installed,
so pin it explicitly: `uv python install 3.12` and `requires-python = ">=3.12,<3.13"` in
`pyproject.toml`.

Once the skeleton (step 2) exists, the expected commands are:

```bash
uv sync --all-extras                 # install deps + dev deps
uv run pytest                        # all tests
uv run pytest tests/test_sync_diff.py::test_idempotent_second_run   # a single test
uv run pytest --cov=src/itmosync --cov-report=term-missing
uv run ruff check src tests
uv run ruff format src tests
uv run mypy --strict src             # must be clean on all of src/
uv run itmosync <command>            # the CLI itself
```

`ruff` and `mypy --strict` passing on the entire `src/` is a hard acceptance criterion, not a
nice-to-have. Core coverage (`sync.py`, `ical/mapper.py`) must be ≥ 90%.

## Architecture

Data flows in one direction and each layer knows nothing about the next:

```
itmo/auth.py     refresh token -> access token (Keychain in, nothing out)
itmo/client.py   the ONLY module containing my.itmo URLs; raw HTTP + retries
itmo/schedule.py raw JSON -> validated pydantic -> list[Lesson]
models.py        Lesson (frozen dataclass), LessonType, SyncPlan, SyncResult
ical/mapper.py   Lesson -> VEVENT (pure; no network)
ical/icloud.py   CalDAV: read window, create/update/delete
sync.py          the diff engine: desired vs existing -> plan -> apply
report.py        SyncResult -> rich output
cli.py           typer commands: parse args, call, print. No logic.
```

`sync.py` must not know how the report is rendered — a future Telegram bot is explicitly out of
scope but must not require rewriting the engine.

### The two identity keys (the central design decision)

These are different keys and must never be conflated:

- **UID** — the stable anchor: `sha1("{date}|{normalized subject}|{type}|{index}")[:16]`, wrapped
  as `itmo-lesson-<16 hex>@itmosync`. Time, room, building, teacher and links are deliberately
  **not** part of it, so a room change updates the event in place.
- **X-ITMO-HASH** — the content hash over start/end/subject/raw_type/room/building/teacher/
  online_url, stored as an X-property inside the VEVENT itself. It answers "does this event need
  updating?".

Because the hash lives in the event, **the calendar is the state store**. There is no SQLite and
none is needed in this version.

`Lesson.index` is the ordinal of a lesson among same (date, subject, type) lessons that day,
assigned after sorting by start time. Without it two lectures of one subject on one day collapse
into a single UID.

### Safety guards (all three are mandatory, all run before any write)

1. Empty `desired` while `existing` is non-empty aborts the run — protects against a broken API or
   a stale token returning `200` with an empty body wiping the calendar. Only `--force` overrides.
2. `len(to_delete) / max(len(existing), 1) > delete_threshold` (default 0.5) aborts and prints what
   would have been deleted.
3. Scope: reads, writes and deletes are confined to the window **and** to UIDs matching
   `^itmo-lesson-[0-9a-f]{16}@itmosync$`. Manually created events in the same calendar are invisible
   to the tool.

## Invariants

- my.itmo URLs live **only** in `itmo/client.py`. Not one address string anywhere else.
- The calendar package is named `ical`, never `calendar` — `calendar` shadows the stdlib module.
- Every deletion goes through **one** function that checks the UID mask. A second deletion path in
  the code is a bug.
- All datetimes are timezone-aware (`Europe/Moscow`, real `VTIMEZONE` written). Naive `datetime` is
  forbidden; floating times in VEVENTs are forbidden.
- Secrets live only in the macOS Keychain under service `itmosync` (`itmo_refresh_token`,
  `apple_app_password` — an Apple *app-specific* password). Never in the config file, logs, report
  output or error messages. The logger masks any token-looking string over 20 chars to `abcd…wxyz`.
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
- **All technical documentation is in English**: this file, the README, everything under `docs/`,
  docstrings, code comments, identifiers and commit messages.
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

## Working order

Follow §14 of the spec; each step ends green (ruff + mypy + pytest) and gets exactly one commit:

1. **API reconnaissance — write no code until this is done.** Record the schedule endpoint, query
   params, headers, the ITMO ID token endpoint, and the exact lesson-type strings in
   `docs/api-notes.md`; save an anonymized response to `tests/fixtures/schedule_sample.json`.
   Pydantic schemas are written against the recorded response, never against a guess.
2. Skeleton — `pyproject.toml`, packages, `config.py`, `errors.py`, `logging.py`, `init`, empty `doctor`.
3. Auth — `itmo/auth.py`, `auth` commands, respx tests.
4. Schedule reading — `itmo/client.py`, `itmo/schedule.py`, `models.py`, `show` (parser verifiable by eye before any CalDAV).
5. `ical/mapper.py` + `test_mapper_golden`.
6. `ical/icloud.py` + `doctor` iCloud checks.
7. `sync.py`, guards, `report.py`, `sync` — plus the full test list in §13.
8. README.

The first run against the live calendar is `--dry-run` only.

The key test of the project is `test_idempotent_second_run`: two runs over identical data, the
second producing an empty plan. CalDAV is never hit in tests — use an in-memory fake client with
the same interface.
