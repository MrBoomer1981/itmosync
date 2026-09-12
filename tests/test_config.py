"""Tests for config loading and secret masking."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from itmosync.config import DEFAULT_LESSON_TYPES, load_config, render_config, write_config
from itmosync.errors import ConfigError
from itmosync.logging import SecretMaskingFilter, mask_secrets


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_render_and_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "config.toml"
    write_config(path, render_config(apple_id="user@example.com", calendar_name="ИТМО · Пары"))

    config = load_config(path)

    assert config.calendar.apple_id == "user@example.com"
    assert config.calendar.name == "ИТМО · Пары"
    assert config.sync.window_days == 28
    assert config.sync.reminder_minutes == 0
    assert config.lesson_types == DEFAULT_LESSON_TYPES
    assert str(config.tz) == "Europe/Moscow"


def test_missing_config_points_at_init(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="itmosync init"):
        load_config(tmp_path / "absent.toml")


def test_invalid_timezone_is_reported(tmp_path: Path) -> None:
    path = _write(tmp_path, '[calendar]\napple_id = "u@e.com"\ntimezone = "Mars/Olympus"\n')

    with pytest.raises(ConfigError, match="timezone"):
        load_config(path)


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, '[calendar]\napple_id = "u@e.com"\nreminder = 3\n')

    with pytest.raises(ConfigError, match=r"calendar\.reminder"):
        load_config(path)


def test_broken_toml_is_reported(tmp_path: Path) -> None:
    path = _write(tmp_path, "[calendar\napple_id =\n")

    with pytest.raises(ConfigError, match="Не удалось прочитать конфиг"):
        load_config(path)


def test_delete_threshold_out_of_range(tmp_path: Path) -> None:
    path = _write(tmp_path, '[calendar]\napple_id = "u@e.com"\n\n[sync]\ndelete_threshold = 1.5\n')

    with pytest.raises(ConfigError, match="delete_threshold"):
        load_config(path)


@pytest.mark.parametrize(
    "text",
    [
        "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27u",
        "Bearer abcdefghijklmnopqrstuvwxyz0123456789",
        "пароль приложения abcd-efgh-ijkl-mnop готов",
    ],
)
def test_secrets_are_masked(text: str) -> None:
    masked = mask_secrets(text)

    assert "…" in masked
    for secret in (
        "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9",
        "abcdefghijklmnopqrstuvwxyz0123456789",
        "abcd-efgh-ijkl-mnop",
    ):
        assert secret not in masked


def test_masking_keeps_paths_and_short_words_readable() -> None:
    text = "Конфиг /Users/student/.config/itmosync/config.toml прочитан за 12 мс"

    assert mask_secrets(text) == text


def test_filter_masks_message_and_args() -> None:
    record = logging.LogRecord(
        name="itmosync",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="токен %s принят",
        args=("abcdefghijklmnopqrstuvwxyz0123456789",),
        exc_info=None,
    )

    assert SecretMaskingFilter().filter(record) is True
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in record.getMessage()
    assert "…" in record.getMessage()
