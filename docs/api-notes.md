# my.itmo API notes

Recorded on 2026-09-12 from a live session at `https://my.itmo.ru/schedule` (Nuxt SPA, axios).
This file is the source of truth for the pydantic schemas. Where it disagrees with
`itmosync-tz.md`, this file wins and the spec gets amended.

Nothing here is a secret: no token values, ISU number, real names or group are recorded.

## Schedule endpoint

```
GET https://my.itmo.ru/api/schedule/schedule/personal?date_start=2026-09-07&date_end=2026-10-11
```

- Both dates are `YYYY-MM-DD`, inclusive on both ends.
- A 35-day window returned fine, so the 28-day window from the spec needs no paging.
- The response always contains one entry per calendar day in the range, including empty days.

### Required headers

| Header | Value | Notes |
|---|---|---|
| `Authorization` | `Bearer <access token>` | |
| `Accept-Language` | `ru` | **Mandatory, see below** |
| `Accept` | `application/json, text/plain, */*` | axios default |

**`Accept-Language: ru` is not optional.** Without it the request still returns `200` and the
full structure, but `subject`, `type` and `work_type` come back as `null` on every lesson, while
`work_type_id` keeps its value. This is silent degradation, not an error — the client must always
send the header, and `schedule.py` must treat a `null` `subject` as a schema error rather than
mapping it to an empty string.

## Response shape

```jsonc
{
  "code": 0,
  "data": [ /* one object per day */ ],
  "message": null
}
```

### Day object

| Field | Type | Notes |
|---|---|---|
| `date` | `str` | `YYYY-MM-DD` |
| `day_number` | `int` | `0` = Sunday, `1` = Monday … `6` = Saturday |
| `week_number` | `int` | Academic week number |
| `note` | `str \| null` | Always `null` in the observed window |
| `type` | `str \| null` | Always `null` in the observed window |
| `lessons` | `list` | Empty list on free days |
| `intersections` | `list` | Empty list everywhere in the observed window |

### Lesson object

Counts below are "non-empty / total" over a 100-lesson, 35-day window.

| Field | Type | Presence | Notes |
|---|---|---|---|
| `pair_id` | `int` | 100/100 | Per-occurrence id; **changes between fetches of the same lesson is unverified — do not use it as the UID** |
| `subject` | `str` | 100/100 | Requires `Accept-Language` |
| `subject_id` | `int` | 100/100 | |
| `type` | `str` | 100/100 | Identical to `work_type` in every observed row |
| `work_type` | `str` | 100/100 | The lesson-type string the config maps |
| `work_type_id` | `int` | 100/100 | Stable numeric type id, see table below |
| `time_start` | `str` | 100/100 | `HH:MM`, local Moscow time, no timezone marker |
| `time_end` | `str` | 100/100 | `HH:MM` |
| `teacher_name` | `str \| null` | 95/100 | Missing on some lab sessions |
| `teacher_id` | `int \| null` | 95/100 | `null` exactly when `teacher_name` is `null` |
| `room` | `str` | 100/100 | Free-form: `"4213"`, `"2407 (архив)"`, `"Переговорная 1 (1311)"` |
| `building` | `str` | 100/100 | **Contains the street address**, e.g. `"ул.Ломоносова, д.9, лит. Б"` |
| `format` | `str` | 100/100 | See format table |
| `format_id` | `int` | 100/100 | |
| `group` | `str` | 100/100 | |
| `note` | `str \| null` | 6/100 | Free text; **may contain a teacher's name** |
| `zoom_url` | `str \| null` | 1/100 | Only meeting link seen |
| `zoom_password` | `str \| null` | 0/100 | Always `null` in the observed window |
| `zoom_info` | `str \| null` | 0/100 | Always `null` in the observed window |
| `flow_type_id`, `flow_id` | `int` | 100/100 | |
| `bld_id`, `main_bld_id` | `int` | 100/100 | |
| `teacher_lesson` | `bool` | present on 1 lesson only | Appears only on the `Встреча` row |

`teacher_lesson` shows that the object is **not** a fixed set of keys: the pydantic model must
ignore unknown fields rather than forbid them.

### Lesson types

`work_type_id` → `work_type`, observed over 35 days:

| id | string |
|---|---|
| 1 | `Лекции` |
| 2 | `Лабораторные занятия` |
| 3 | `Практические занятия` |
| 131 | `Встреча` |

Exams and consultations did not occur in the window; their strings are still unknown.

### Formats

| `format_id` | `format` |
|---|---|
| 1 | `"Очный"` |
| 2 | `"Очно - дистанционный "` |

Note the **trailing space** in `"Очно - дистанционный "`. Compare on a stripped value.

`format_id = 2` is used for most lessons regardless of whether a `zoom_url` exists, so it does
**not** mean "online". A lesson is online only when `zoom_url` is non-null.

## Authentication

ITMO ID is Keycloak, realm `itmo`.

| | |
|---|---|
| Client id | `student-personal-cabinet` (public client, no secret) |
| Token endpoint | `https://id.itmo.ru/auth/realms/itmo/protocol/openid-connect/token` |
| Discovery | `https://id.itmo.ru/auth/realms/itmo/.well-known/openid-configuration` |
| Scope | `openid profile` |
| Access token lifetime | 1800 s (30 min) |
| Refresh token lifetime | ~30 days (observed expiry was now + 30 days) |

### Where the tokens live in the browser

| Cookie | Content |
|---|---|
| `auth._token.itmoId` | **Access** token, stored *with* the `Bearer ` prefix (~1100 chars) |
| `auth._refresh_token.itmoId` | **Refresh** token, raw JWT, *no* prefix (~700 chars) |
| `auth._token_expiration.itmoId` | Epoch milliseconds |
| `auth._refresh_token_expiration.itmoId` | Epoch milliseconds |

**The spec's snippet reads the wrong cookie.** `itmosync-tz.md` §5 extracts
`auth._token.itmoId`, which is the 30-minute access token. The refresh token is in
`auth._refresh_token.itmoId`. The correct browser-console snippet is:

```js
decodeURIComponent(document.cookie.split('auth._refresh_token.itmoId=')[1].split(';')[0])
```

`auth --set-token` should still strip a leading `Bearer ` defensively, in case a user pastes the
access-token cookie by mistake, and should reject a value that is not a three-part JWT.

### Refresh exchange — NOT yet verified live

Expected, from the Keycloak discovery document:

```
POST https://id.itmo.ru/auth/realms/itmo/protocol/openid-connect/token
Content-Type: application/x-www-form-urlencoded

grant_type=refresh_token&client_id=student-personal-cabinet&refresh_token=<token>
```

Expected response fields: `access_token`, `expires_in`, `refresh_token`, `refresh_expires_in`,
`token_type`, `scope`.

This exchange was deliberately **not** executed during reconnaissance: Keycloak may rotate the
refresh token, which would have invalidated the browser session. Verify it at step 3, and confirm
there whether a rotated `refresh_token` comes back — if it does, it must be written straight back
into the Keychain.

## Other endpoints seen

- `GET /api/schedule/meta/time_slots` — the bell schedule. Not needed: every lesson already
  carries `time_start` / `time_end`.
- `POST /api/flagsmith/identities/`, `GET /api/system/v1/menu/items` — unrelated to schedules.

## Consequences for the implementation

1. `Accept-Language: ru` goes into the client's default headers, with a comment explaining why.
2. There is no `address` field. `Lesson.address` from the spec has no source and is dropped;
   `building` already holds the address.
3. `LessonType` mapping keys on the exact `work_type` strings above. `Лабораторные занятия`
   replaces the spec's guess `Лабораторные работы`, and `Встреча` is a real type the spec missed.
4. Empty days come back as day objects with `lessons: []`. "API returned an empty schedule" for
   the safety guard means *no lessons across all days*, not an empty `data` array.
5. `zoom_password` and `zoom_info` exist in the payload but were never populated; parse them as
   optional and ignore them in the mapper until a real value shows up.

## Fixture

`tests/fixtures/schedule_sample.json` is the real response for `2026-09-07 … 2026-09-13`, with
teacher names and ids replaced by invented ones, the group replaced with `X31234`, the `note`
that contained a teacher's name replaced, and the Zoom link replaced with a dummy. It covers all
four lesson types, both formats, a lesson without a teacher, a lesson with a Zoom link, two empty
days, and two pairs of same-subject same-type lessons on one day (2026-09-07 English, 2026-09-12
Python) — the case `Lesson.index` exists for.
