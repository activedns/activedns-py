from __future__ import annotations

import platform
import random
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ._errors import (
    ActiveDNSError,
    APIError,
    ForbiddenError,
    RateLimitError,
    TransportError,
    UnauthorizedError,
)
from ._models import Page, RateLimit
from ._ratelimit import exhausted_until, parse_rate_limits
from ._token import EMBEDDED_TOKEN
from ._version import __version__

DEFAULT_BASE_URL = "https://activedns.net"
"""The server used unless ``base_url`` is given."""

_QUERY_PATH = "/api/v2/query"
_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 30.0
# statuses that mean "try again later"
_RETRYABLE = frozenset({429, 502, 503, 504})


class _Retry(Exception):
    """A failed attempt that may be tried again, after at least ``after`` seconds."""

    def __init__(self, error: ActiveDNSError, after: float = 0.0) -> None:
        self.error = error
        self.after = after


class _BaseClient:
    """What Client and AsyncClient share: everything but sending and sleeping."""

    def __init__(
        self,
        app: str,
        *,
        token: str | None,
        base_url: str,
        max_concurrent: int,
        max_retries: int,
        max_wait: float,
    ) -> None:
        app = app.strip()
        if not app:
            raise ValueError('app is required, as in Client("mytool/1.0")')
        if not all(" " <= c <= "~" for c in app):
            raise ValueError(f"app {app!r}: only printable ASCII fits in a User-Agent")
        token = EMBEDDED_TOKEN if token is None else token.strip()
        if not token:
            raise ValueError("token is empty")
        url = httpx.URL(base_url)
        if url.scheme not in ("http", "https") or not url.host:
            raise ValueError(f"invalid base URL {base_url!r}")
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        if max_retries < 0 or max_wait < 0:
            raise ValueError("max_retries and max_wait must not be negative")

        self._url = str(url).rstrip("/") + _QUERY_PATH
        self._headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": f"{app} activedns-py/{__version__} "
            f"(python {platform.python_version()}; {platform.system().lower()})",
            "Accept": "application/json",
        }
        self._max_retries = max_retries
        self._max_wait = max_wait

        # held for a moment at a time, and never while waiting
        self._lock = threading.Lock()
        # nothing is sent before this (after a "slow down")
        self._not_before: datetime | None = None
        # a limit is reached: nothing is sent until its retry_at
        self._limited: RateLimitError | None = None
        # the token was refused: nothing is sent again
        self._refused: UnauthorizedError | None = None
        self._limits: tuple[RateLimit, ...] = ()

        # replaced in tests
        self._now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
        self._jitter: Callable[[], float] = random.random

    @property
    def rate_limits(self) -> tuple[RateLimit, ...]:
        """The rate limits reported by the most recent response."""
        with self._lock:
            return self._limits

    @staticmethod
    def _combined(domain: str | None, ip: str | None, asn: int | None) -> dict[str, Any]:
        """The parameters of a combined query."""
        params: dict[str, Any] = {}
        if domain:
            params["domain"] = domain
        if ip:
            params["ip"] = ip
        if asn:
            params["asn"] = f"AS{asn}"
        if not params:
            raise ValueError("a combined query needs at least one of domain, ip and asn")
        return params

    @staticmethod
    def _paged(params: dict[str, Any], page_size: int | None, cursor: int | None) -> dict[str, Any]:
        if page_size is not None:
            params["limit"] = page_size  # the API's name for the page size
        if cursor is not None:
            params["cursor"] = cursor
        return params

    @staticmethod
    def _after(page: Page) -> dict[str, Any] | None:
        """The request for the page after ``page``, or ``None`` after the last one."""
        if page._request is None:
            raise ValueError("next_page needs a page returned by this package")
        if not page.has_more:
            return None
        return dict(page._request, cursor=page.next_cursor)

    def _ready(self) -> float:
        """Seconds to wait before the client may send. Raises the reason it will not."""
        with self._lock:
            now = self._now()
            if self._refused is not None:
                raise self._refused
            limited = self._limited
            if limited is not None:
                if limited.retry_at is None or now < limited.retry_at:
                    raise RateLimitError(
                        limited.message, exceeded=limited.exceeded, retry_at=limited.retry_at, local=True
                    )
                self._limited = None
            return (self._not_before - now).total_seconds() if self._not_before else 0.0

    def _retry_later(self, retry: _Retry, attempt: int) -> None:
        """Hold the client back until the next attempt, or raise when there is none."""
        if attempt >= self._max_retries or retry.after > self._max_wait:
            self._give_up(retry.error)
            raise retry.error from retry.__cause__
        wait = min(self._backoff(attempt), self._max_wait)
        if retry.after > 0:
            # up to a second later than asked, so that the clients
            # sharing a limit do not all come back at the same instant
            wait = max(wait, retry.after + self._jitter())
        self._pause(wait)

    def _pause(self, seconds: float) -> None:
        """Hold every request of the client back for a while."""
        self._not_earlier_than(self._now() + timedelta(seconds=seconds))

    def _not_earlier_than(self, moment: datetime) -> None:
        with self._lock:
            if self._not_before is None or moment > self._not_before:
                self._not_before = moment

    def _hold_until(self, moment: datetime, limit: RateLimitError) -> None:
        """Keep the client from sending before a moment.

        Requests wait when that is at most ``max_wait`` away, and otherwise
        fail at once with ``limit``.
        """
        if (moment - self._now()).total_seconds() > self._max_wait:
            with self._lock:
                self._limited = limit
        else:
            self._not_earlier_than(moment)

    def _give_up(self, error: ActiveDNSError) -> None:
        """Remember when the server accepts requests again, for later calls."""
        if isinstance(error, RateLimitError) and error.retry_at is not None:
            self._hold_until(error.retry_at, error)

    def _backoff(self, attempt: int) -> float:
        """Seconds before retry number attempt+1: 1, 2, 4 … up to 30, upper half randomised."""
        delay = min(_BACKOFF_BASE * 2.0 ** min(attempt, 10), _BACKOFF_CAP)
        return delay / 2 + self._jitter() * delay / 2

    def _note_rate_limits(self, limits: tuple[RateLimit, ...]) -> None:
        """Remember the limits of a response, and wait out one with nothing left.

        The request that would have been refused is then not sent.
        """
        if not limits:
            return
        with self._lock:
            self._limits = limits
        exhausted = exhausted_until(limits, self._now())
        if exhausted is not None:
            until, name = exhausted
            self._hold_until(until, RateLimitError("rate limit reached", exceeded=name, retry_at=until))

    @staticmethod
    def _unanswered(exc: httpx.TransportError) -> Exception:
        """The error for a request that got no response."""
        if isinstance(exc, httpx.TimeoutException):
            # A request that timed out was probably an expensive search still
            # running on the server: sending it again would double the load.
            return TransportError(f"request timed out: {exc}")
        return _Retry(TransportError(str(exc) or type(exc).__name__))

    def _read(self, response: httpx.Response) -> dict[str, Any]:
        """Decode a response. Raises _Retry when trying again can help."""
        now = self._now()
        limits = parse_rate_limits(response.headers, now)
        self._note_rate_limits(limits)

        if response.status_code == 200:
            try:
                data = response.json()
            except ValueError as exc:
                raise ActiveDNSError(f"decoding the response: {exc}") from exc
            if not isinstance(data, dict):
                raise ActiveDNSError("decoding the response: not an object")
            return data

        try:
            problem = response.json()
        except ValueError:
            problem = None
        if not isinstance(problem, dict):
            problem = {}
        message = problem.get("error") or response.reason_phrase
        status = response.status_code

        if status == 401:
            refused = UnauthorizedError(status, message)
            with self._lock:
                self._refused = refused
            raise refused
        if status == 403:
            raise ForbiddenError(status, message)
        if status not in _RETRYABLE:
            raise APIError(status, message)

        after = self._retry_after(response.headers.get("Retry-After"), now)
        if status != 429:
            raise _Retry(APIError(status, message), after)
        if after == 0:
            # no Retry-After: the reset of the limit that ran out says the same
            exhausted = exhausted_until(limits, now)
            if exhausted is not None:
                after = (exhausted[0] - now).total_seconds()
        retry_at = now + timedelta(seconds=after) if after > 0 else None
        exceeded = problem.get("exceeded") or None
        raise _Retry(RateLimitError(message, exceeded=exceeded, retry_at=retry_at), after)

    @staticmethod
    def _retry_after(header: str | None, now: datetime) -> float:
        """Read a Retry-After header: seconds, or an HTTP date."""
        if not header:
            return 0.0
        try:
            return max(float(int(header)), 0.0)
        except ValueError:
            pass
        try:
            return max((parsedate_to_datetime(header) - now).total_seconds(), 0.0)
        except (TypeError, ValueError):
            return 0.0
