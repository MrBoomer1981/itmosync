"""Configuration loading.

The config file holds preferences only. Secrets live in the macOS Keychain and never
appear here — see itmo/auth.py.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from itmosync.errors import ConfigError

DEFAULT_CONFIG_PATH: Final = Path.home() / ".config" / "itmosync" / "config.toml"

# Keys are the exact `work_type` strings the API returns; see docs/api-notes.md.
DEFAULT_LESSON_TYPES: Final[dict[str, str]] = {
    "Лекции": "лек",
    "Практические занятия": "пр",
    "Лабораторные занятия": "лаб",
    "Встреча": "вст",
    "Консультация": "конс",
    "Экзамен": "экз",
}


class CalendarConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "ИТМО · Пары"
    timezone: str = "Europe/Moscow"
    caldav_url: str = "https://caldav.icloud.com/"
    apple_id: str

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"неизвестная таймзона {value!r}") from exc
        return value


class SyncConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_days: int = Field(default=28, ge=1, le=365)
    reminder_minutes: int = Field(default=0, ge=0)
    online_link_in_location: bool = True
    delete_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    notify: bool = False


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calendar: CalendarConfig
    sync: SyncConfig = SyncConfig()
    lesson_types: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_LESSON_TYPES))

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.calendar.timezone)


def load_config(path: Path | None = None) -> Config:
    """Read and validate the config file."""
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        raise ConfigError(f"Конфиг не найден: {path}\nСоздайте его командой: itmosync init")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Не удалось прочитать конфиг {path}: {exc}") from exc

    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        problems = "\n".join(
            f"  {'.'.join(str(p) for p in error['loc']) or '<корень>'}: {error['msg']}"
            for error in exc.errors()
        )
        raise ConfigError(f"Конфиг {path} заполнен неверно:\n{problems}") from exc


def render_config(apple_id: str, calendar_name: str) -> str:
    """Render a fresh config file with the defaults from the spec."""
    defaults = SyncConfig()
    calendar = CalendarConfig(apple_id=apple_id, name=calendar_name)
    types = "\n".join(f'"{key}" = "{value}"' for key, value in DEFAULT_LESSON_TYPES.items())
    return f"""[calendar]
name = "{calendar.name}"
timezone = "{calendar.timezone}"
caldav_url = "{calendar.caldav_url}"
apple_id = "{calendar.apple_id}"

[sync]
window_days = {defaults.window_days}
reminder_minutes = {defaults.reminder_minutes}      # 0 = напоминаний нет
online_link_in_location = {str(defaults.online_link_in_location).lower()}
delete_threshold = {defaults.delete_threshold}
notify = {str(defaults.notify).lower()}   # уведомление macOS после прогона

[lesson_types]
{types}
"""


def write_config(path: Path, content: str) -> None:
    """Write a config file, creating its directory."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Не удалось записать конфиг {path}: {exc}") from exc
