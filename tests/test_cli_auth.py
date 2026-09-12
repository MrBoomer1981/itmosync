"""Tests for the token-entry paths of the auth command."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from itmosync import cli
from itmosync.errors import AuthError

runner = CliRunner()

VALID = "ey" + "J0eXAiOiJKV1QifQ" + ".eyJzdWIiOiIxIn0" + ".c2lnbmF0dXJlX3ZhbHVl"


class StubStore:
    saved: ClassVar[list[str]] = []

    def set_refresh_token(self, token: str) -> None:
        StubStore.saved.append(token)

    def set_apple_password(self, password: str) -> None:
        StubStore.saved.append(password)


class StubAuthenticator:
    def __init__(self, *args: object, **kwargs: object) -> None: ...

    def __enter__(self) -> StubAuthenticator:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def refresh(self) -> None:
        return None


@pytest.fixture(autouse=True)
def stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    StubStore.saved = []
    monkeypatch.setattr(cli, "SecretStore", StubStore)
    monkeypatch.setattr(cli, "Authenticator", StubAuthenticator)


def test_bookmarklet_is_printed_and_is_self_contained() -> None:
    result = runner.invoke(cli.app, ["auth", "--bookmarklet"])

    assert result.exit_code == 0
    assert "Закладка" in result.output
    assert "--from-clipboard" in result.output
    # Asserted against the constant, not the rendered output: rich wraps the long line.
    assert cli.BOOKMARKLET.startswith("javascript:(function()")
    assert "auth\\._refresh_token\\.itmoId" in cli.BOOKMARKLET
    # The whole point is that it only reads and copies — it must not talk to the network.
    for forbidden in ("fetch(", "XMLHttpRequest", "sendBeacon", "http://", "location.href="):
        assert forbidden not in cli.BOOKMARKLET


def test_token_is_taken_from_the_clipboard_and_the_clipboard_is_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleared: list[bool] = []
    monkeypatch.setattr(cli, "_read_clipboard", lambda: f"  {VALID}  ")
    monkeypatch.setattr(cli, "_clear_clipboard", lambda: cleared.append(True))

    result = runner.invoke(cli.app, ["auth", "--set-token", "--from-clipboard"])

    assert result.exit_code == 0, result.output
    assert StubStore.saved == [VALID]
    assert cleared == [True]
    assert "Буфер обмена очищен" in result.output


def test_clipboard_with_the_access_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grabbing the neighbouring cookie is the likely mistake; it must fail loudly."""
    monkeypatch.setattr(cli, "_read_clipboard", lambda: "Bearer not.a.jwt")
    monkeypatch.setattr(cli, "_clear_clipboard", lambda: None)

    result = runner.invoke(cli.app, ["auth", "--set-token", "--from-clipboard"])

    # CliRunner calls app() directly, so the exception-to-exit-code mapping in main()
    # does not run here; the contract under test is that nothing gets stored.
    assert isinstance(result.exception, AuthError)
    assert result.exception.exit_code == 3
    assert StubStore.saved == []


def test_exactly_one_mode_is_required() -> None:
    both = runner.invoke(cli.app, ["auth", "--set-token", "--set-apple-password"])
    neither = runner.invoke(cli.app, ["auth"])

    assert both.exit_code == 2
    assert neither.exit_code == 2
    assert StubStore.saved == []


def test_stdin_path_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_read_secret", lambda prompt: VALID)

    result = runner.invoke(cli.app, ["auth", "--set-token"])

    assert result.exit_code == 0, result.output
    assert StubStore.saved == [VALID]


def test_clipboard_read_uses_pbpaste(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = VALID

    def fake_run(args: list[str], **kwargs: Any) -> Result:
        calls.append(args)
        return Result()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli._read_clipboard() == VALID
    assert calls == [["pbpaste"]]
