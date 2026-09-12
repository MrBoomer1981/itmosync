"""ITMO ID authentication.

ITMO ID is a Keycloak realm. We never see a password: the user pastes a refresh token
taken from their browser once, we keep it in the macOS Keychain, and exchange it for a
short-lived access token on every run.

The access token is deliberately kept in memory only. It lives 30 minutes, so persisting
it would add a second secret at rest in exchange for nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
import keyring

from itmosync.errors import (
    AuthError,
    ItmoApiError,
    RateLimitError,
    TokenExpiredError,
    TokenRejectedError,
)
from itmosync.itmo.client import request_with_retries
from itmosync.logging import get_logger

# ITMO ID, not my.itmo: the rule that my.itmo URLs live only in client.py does not apply.
TOKEN_URL: Final = "https://id.itmo.ru/auth/realms/itmo/protocol/openid-connect/token"
CLIENT_ID: Final = "student-personal-cabinet"

KEYRING_SERVICE: Final = "itmosync"
KEY_REFRESH_TOKEN: Final = "itmo_refresh_token"
KEY_APPLE_PASSWORD: Final = "apple_app_password"

TIMEOUT: Final = 15.0
# Refresh a little early so a token cannot expire between the check and the request.
EXPIRY_MARGIN: Final = timedelta(seconds=60)

_log = get_logger()


def normalize_refresh_token(raw: str) -> str:
    """Clean up a token pasted by hand and reject what is obviously not one.

    Users paste from a browser console, so leading/trailing whitespace and quotes are
    common, and it is easy to grab the access-token cookie by mistake — that one carries
    a `Bearer ` prefix.
    """
    token = raw.strip().strip("\"'").strip()
    if token.lower().startswith("bearer "):
        token = token[len("bearer ") :].strip()
    if token.count(".") != 2 or not token.startswith("ey"):
        raise AuthError(
            "Это не похоже на refresh-токен: ожидается JWT из трёх частей, "
            "начинающийся с «ey».\n"
            "Проверьте, что скопирована именно cookie auth._refresh_token.itmoId, "
            "а не auth._token.itmoId."
        )
    return token


class SecretStore:
    """The macOS Keychain, under the `itmosync` service."""

    def __init__(self, service: str = KEYRING_SERVICE) -> None:
        self._service = service

    def _get(self, key: str, human: str, command: str) -> str:
        value = keyring.get_password(self._service, key)
        if not value:
            raise AuthError(
                f"{human} не найден в связке ключей.\nДобавьте его: itmosync auth {command}"
            )
        return value

    def get_refresh_token(self) -> str:
        return self._get(KEY_REFRESH_TOKEN, "Refresh-токен my.itmo", "--set-token")

    def set_refresh_token(self, token: str) -> None:
        keyring.set_password(self._service, KEY_REFRESH_TOKEN, token)

    def get_apple_password(self) -> str:
        return self._get(KEY_APPLE_PASSWORD, "Пароль приложения Apple ID", "--set-apple-password")

    def set_apple_password(self, password: str) -> None:
        keyring.set_password(self._service, KEY_APPLE_PASSWORD, password)


@dataclass(frozen=True, slots=True)
class AccessToken:
    """A short-lived access token and the moment it stops being usable."""

    value: str
    expires_at: datetime

    def is_fresh(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        return now + EXPIRY_MARGIN < self.expires_at


class Authenticator:
    """Exchanges the stored refresh token for access tokens."""

    def __init__(
        self, store: SecretStore | None = None, client: httpx.Client | None = None
    ) -> None:
        self._store = store or SecretStore()
        self._client = client or httpx.Client(timeout=TIMEOUT)
        self._owns_client = client is None
        self._access: AccessToken | None = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Authenticator:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def access_token(self) -> str:
        """Return a usable access token, refreshing it only when needed."""
        if self._access is not None and self._access.is_fresh():
            return self._access.value
        return self.refresh().value

    def invalidate(self) -> None:
        """Forget the cached access token so the next call exchanges a fresh one."""
        self._access = None

    def refresh(self) -> AccessToken:
        """Exchange the refresh token for a new access token."""
        payload = self._post(
            {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": self._store.get_refresh_token(),
            }
        )

        access = payload.get("access_token")
        if not isinstance(access, str) or not access:
            raise AuthError("ITMO ID не вернул access_token — ответ не распознан.")

        expires_in = payload.get("expires_in")
        seconds = expires_in if isinstance(expires_in, int) else 1800
        token = AccessToken(value=access, expires_at=datetime.now(UTC) + timedelta(seconds=seconds))

        # Keycloak may rotate the refresh token. Missing this would break the *next* run,
        # long after the change that caused it.
        rotated = payload.get("refresh_token")
        if isinstance(rotated, str) and rotated:
            self._store.set_refresh_token(rotated)
            _log.debug("refresh-токен обновлён провайдером")

        self._access = token
        return token

    def _post(self, data: dict[str, str]) -> dict[str, Any]:
        response = request_with_retries(
            self._client,
            "POST",
            TOKEN_URL,
            data=data,
            error_factory=lambda detail: AuthError(f"ITMO ID недоступен {detail}"),
        )
        if response.status_code >= 400:
            raise self._client_error(response)
        return self._parse(response)

    @staticmethod
    def _parse(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise AuthError("ITMO ID вернул не JSON — вероятно, сменился адрес входа.") from exc
        if not isinstance(payload, dict):
            raise AuthError("ITMO ID вернул неожиданную структуру ответа.")
        return payload

    @staticmethod
    def _client_error(response: httpx.Response) -> AuthError | ItmoApiError:
        if response.status_code == 429:
            return RateLimitError("ITMO ID просит подождать: слишком много запросов.")
        try:
            body = response.json()
            code = body.get("error") if isinstance(body, dict) else None
        except ValueError:
            code = None

        if code in {"invalid_grant", "invalid_token"}:
            return TokenExpiredError()
        return TokenRejectedError(
            f"ITMO ID отклонил токен (HTTP {response.status_code}"
            + (f", {code}" if code else "")
            + ").\n"
            + TokenExpiredError.instructions()
        )
