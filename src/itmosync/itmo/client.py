"""HTTP access to my.itmo.

This is the only module in the package allowed to contain a my.itmo URL, and it is also
where the shared retry policy lives so that auth.py and schedule fetching behave the same
way under a flaky network.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final

import httpx

from itmosync.errors import ItmoApiError, ItmosyncError, RateLimitError
from itmosync.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - import only for typing, auth.py imports us at runtime
    from itmosync.itmo.auth import Authenticator

BASE_URL: Final = "https://my.itmo.ru"
SCHEDULE_PATH: Final = "/api/schedule/schedule/personal"

TIMEOUT: Final = 15.0
MAX_ATTEMPTS: Final = 3
BACKOFF_BASE: Final = 0.5

_log = get_logger()


def request_with_retries(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    error_factory: Callable[[str], ItmosyncError],
    **kwargs: Any,
) -> httpx.Response:
    """Send a request, retrying server and network failures but never client errors.

    Returns the first response with a status below 500. A 4xx is the caller's business:
    retrying a rejected token or a bad request only wastes time and rate limit.
    """
    last_error: str = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            last_error = str(exc) or exc.__class__.__name__
            _log.debug("сеть недоступна (попытка %s/%s): %s", attempt, MAX_ATTEMPTS, last_error)
        else:
            if response.status_code < 500:
                return response
            last_error = f"HTTP {response.status_code}"
            _log.debug("сервер ответил %s (попытка %s/%s)", last_error, attempt, MAX_ATTEMPTS)

        if attempt < MAX_ATTEMPTS:
            time.sleep(BACKOFF_BASE * 2 ** (attempt - 1))

    raise error_factory(f"после {MAX_ATTEMPTS} попыток: {last_error}")


class ItmoClient:
    """Authenticated calls to the my.itmo API."""

    def __init__(
        self,
        authenticator: Authenticator,
        client: httpx.Client | None = None,
    ) -> None:
        self._auth = authenticator
        self._client = client or httpx.Client(timeout=TIMEOUT, base_url=BASE_URL)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
        self._auth.close()

    def __enter__(self) -> ItmoClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._auth.access_token()}",
            # Without this the API answers 200 but nulls out subject, type and work_type.
            # See docs/api-notes.md.
            "Accept-Language": "ru",
            "Accept": "application/json, text/plain, */*",
        }

    def fetch_schedule(self, date_start: dt.date, date_end: dt.date) -> dict[str, Any]:
        """Return the raw personal schedule payload for an inclusive date range."""
        params = {"date_start": date_start.isoformat(), "date_end": date_end.isoformat()}
        response = self._get(SCHEDULE_PATH, params)

        if response.status_code == 401:
            # The access token died mid-run; exchange a fresh one and try exactly once more.
            _log.debug("access-токен отвергнут, обновляю и повторяю запрос")
            self._auth.invalidate()
            response = self._get(SCHEDULE_PATH, params)

        if response.status_code == 429:
            raise RateLimitError("my.itmo просит подождать: слишком много запросов.")
        if response.status_code >= 400:
            raise ItmoApiError(
                f"my.itmo ответил {response.status_code} на запрос расписания "
                f"за {params['date_start']} — {params['date_end']}."
            )

        payload = self._decode(response)
        return payload

    def _get(self, path: str, params: Mapping[str, str]) -> httpx.Response:
        return request_with_retries(
            self._client,
            "GET",
            path,
            headers=self._headers(),
            params=dict(params),
            error_factory=lambda detail: ItmoApiError(f"my.itmo недоступен {detail}"),
        )

    @staticmethod
    def _decode(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ItmoApiError("my.itmo вернул не JSON — возможно, сменился адрес API.") from exc
        if not isinstance(payload, dict):
            raise ItmoApiError("my.itmo вернул неожиданную структуру ответа.")
        return payload
