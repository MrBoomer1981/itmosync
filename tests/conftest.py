"""Shared fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def schedule_payload() -> dict[str, Any]:
    """The real one-week response recorded during API reconnaissance (anonymized)."""
    payload: dict[str, Any] = json.loads((FIXTURES / "schedule_sample.json").read_text("utf-8"))
    return payload


@pytest.fixture
def config() -> Any:
    from itmosync.config import Config

    return Config.model_validate({"calendar": {"apple_id": "student@icloud.com"}})


@pytest.fixture(autouse=True)
def stamp_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Keep every test away from the real ~/.local/state: a real sync writes the stamp."""
    path = tmp_path / "state" / "last-sync"
    monkeypatch.setattr("itmosync.runstamp.STAMP_PATH", path)
    return path
