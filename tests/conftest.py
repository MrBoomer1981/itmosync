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
