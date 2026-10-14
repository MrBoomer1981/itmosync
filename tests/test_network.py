"""Tests for wait_for_network: the pause after wake before a daily sync starts."""

from __future__ import annotations

import httpx
import pytest
import respx

from itmosync.errors import NetworkUnavailableError
from itmosync.itmo import client as client_module
from itmosync.itmo.client import BASE_URL, wait_for_network


class FakeClock:
    """Stands in for the time module: sleeping advances the clock instantly."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(client_module, "time", fake)
    return fake


@respx.mock
def test_returns_at_once_when_the_network_is_up(clock: FakeClock) -> None:
    respx.head(f"{BASE_URL}/").mock(return_value=httpx.Response(200))

    wait_for_network()

    assert clock.sleeps == []


@respx.mock
def test_any_http_answer_means_the_network_is_up(clock: FakeClock) -> None:
    """A 503 is the API's problem, not the network's — the sync's own errors report it."""
    respx.head(f"{BASE_URL}/").mock(return_value=httpx.Response(503))

    wait_for_network()

    assert clock.sleeps == []


@respx.mock
def test_waits_for_wifi_to_come_back(clock: FakeClock) -> None:
    respx.head(f"{BASE_URL}/").mock(
        side_effect=[
            httpx.ConnectError("no route"),
            httpx.ConnectError("no route"),
            httpx.Response(200),
        ]
    )

    wait_for_network(timeout=90, interval=5)

    assert clock.sleeps == [5, 5]


@respx.mock
def test_gives_up_after_the_timeout(clock: FakeClock) -> None:
    route = respx.head(f"{BASE_URL}/").mock(side_effect=httpx.ConnectError("no route"))

    with pytest.raises(NetworkUnavailableError, match="Сети нет") as caught:
        wait_for_network(timeout=20, interval=5)

    assert clock.now <= 20
    assert route.call_count == 5
    assert caught.value.exit_code == 4
