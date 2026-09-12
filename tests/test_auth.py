"""Tests for the ITMO ID refresh-token exchange."""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from typing import Any

import httpx
import keyring
import pytest
import respx

from itmosync.errors import AuthError, RateLimitError, TokenExpiredError, TokenRejectedError
from itmosync.itmo import auth as auth_module
from itmosync.itmo.auth import (
    KEY_REFRESH_TOKEN,
    KEYRING_SERVICE,
    TOKEN_URL,
    Authenticator,
    SecretStore,
    normalize_refresh_token,
)
from itmosync.logging import setup_logging

VALID = "ey" + "J0eXAiOiJKV1QifQ" + ".eyJzdWIiOiIxIn0" + ".c2lnbmF0dXJlX3ZhbHVl"
ROTATED = "ey" + "J0eXAiOiJKV1QifQ" + ".eyJzdWIiOiIyIn0" + ".bmV3X3NpZ25hdHVyZV8x"


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[tuple[str, str], str]]:
    """Replace the macOS Keychain with a dict so tests never touch real secrets."""
    store: dict[tuple[str, str], str] = {}

    def get_password(service: str, key: str) -> str | None:
        return store.get((service, key))

    def set_password(service: str, key: str, value: str) -> None:
        store[(service, key)] = value

    monkeypatch.setattr(keyring, "get_password", get_password)
    monkeypatch.setattr(keyring, "set_password", set_password)
    yield store


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the backoff instant; the delays themselves are not under test."""
    monkeypatch.setattr(auth_module.time, "sleep", lambda _seconds: None)


@pytest.fixture
def store(fake_keyring: dict[tuple[str, str], str]) -> SecretStore:
    fake_keyring[(KEYRING_SERVICE, KEY_REFRESH_TOKEN)] = VALID
    return SecretStore()


def _token_response(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "access_token": "access-value-0123456789",
        "expires_in": 1800,
        "token_type": "Bearer",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (f"  {VALID}  ", VALID),
        (f'"{VALID}"', VALID),
        (f"Bearer {VALID}", VALID),
    ],
)
def test_normalize_accepts_pasted_forms(raw: str, expected: str) -> None:
    assert normalize_refresh_token(raw) == expected


@pytest.mark.parametrize("raw", ["", "not-a-token", "ey.only-two"])
def test_normalize_rejects_nonsense(raw: str) -> None:
    with pytest.raises(AuthError, match="refresh-токен"):
        normalize_refresh_token(raw)


@respx.mock
def test_refresh_returns_access_token(store: SecretStore) -> None:
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=_token_response()))

    with Authenticator(store) as authenticator:
        token = authenticator.refresh()

    assert token.value == "access-value-0123456789"
    assert token.is_fresh()
    sent = dict(pair.split("=", 1) for pair in route.calls[0].request.content.decode().split("&"))
    assert sent["grant_type"] == "refresh_token"
    assert sent["client_id"] == "student-personal-cabinet"


@respx.mock
def test_rotated_refresh_token_is_persisted(
    store: SecretStore, fake_keyring: dict[tuple[str, str], str]
) -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=_token_response(refresh_token=ROTATED))
    )

    with Authenticator(store) as authenticator:
        authenticator.refresh()

    assert fake_keyring[(KEYRING_SERVICE, KEY_REFRESH_TOKEN)] == ROTATED


@respx.mock
def test_access_token_is_cached(store: SecretStore) -> None:
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=_token_response()))

    with Authenticator(store) as authenticator:
        first = authenticator.access_token()
        second = authenticator.access_token()

    assert first == second
    assert route.call_count == 1


@respx.mock
def test_stale_access_token_is_exchanged_again(store: SecretStore) -> None:
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=_token_response(expires_in=1))
    )

    with Authenticator(store) as authenticator:
        authenticator.access_token()
        authenticator.access_token()

    assert route.call_count == 2


@respx.mock
def test_dead_refresh_token_asks_for_a_new_one(store: SecretStore) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))

    with pytest.raises(TokenExpiredError) as excinfo, Authenticator(store) as authenticator:
        authenticator.refresh()

    assert "auth._refresh_token.itmoId" in str(excinfo.value)
    assert "itmosync auth --set-token" in str(excinfo.value)


@respx.mock
def test_other_client_error_is_rejection(store: SecretStore) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401, json={"error": "invalid_client"}))

    with pytest.raises(TokenRejectedError), Authenticator(store) as authenticator:
        authenticator.refresh()


@respx.mock
def test_rate_limit_is_its_own_error(store: SecretStore) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(429))

    with pytest.raises(RateLimitError), Authenticator(store) as authenticator:
        authenticator.refresh()


@respx.mock
def test_client_errors_are_not_retried(store: SecretStore) -> None:
    route = respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "bad"}))

    with pytest.raises(AuthError), Authenticator(store) as authenticator:
        authenticator.refresh()

    assert route.call_count == 1


@respx.mock
def test_server_errors_are_retried_then_succeed(store: SecretStore) -> None:
    route = respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(500),
            httpx.Response(200, json=_token_response()),
        ]
    )

    with Authenticator(store) as authenticator:
        authenticator.refresh()

    assert route.call_count == 3


@respx.mock
def test_network_failure_gives_up_after_three_attempts(store: SecretStore) -> None:
    route = respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("нет сети"))

    with pytest.raises(AuthError, match="недоступен"), Authenticator(store) as authenticator:
        authenticator.refresh()

    assert route.call_count == 3


@respx.mock
def test_tokens_never_reach_the_log(store: SecretStore) -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=_token_response(refresh_token=ROTATED))
    )
    logger = setup_logging(verbose=True)
    buffer = io.StringIO()
    logger.addHandler(logging.StreamHandler(buffer))

    with Authenticator(store) as authenticator:
        authenticator.refresh()
    logger.debug("обмен выполнен для %s", ROTATED)
    logger.debug("access=%s", "access-value-0123456789")

    written = buffer.getvalue()
    assert ROTATED not in written
    assert "access-value-0123456789" not in written
    assert "…" in written
