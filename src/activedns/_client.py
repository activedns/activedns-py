from __future__ import annotations

import platform
import random
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from types import TracebackType
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
from ._models import Page, RateLimit, page_from_dict
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


class Client:
    """An ActiveDNS API client. It is safe to use from several threads.

    Use one client per program: request pacing and rate-limit state are kept
    per client. Close it when done, or use it as a context manager.

    Args:
        app: Name and version of the calling program, such as
            ``"mytool/1.2"``. Sent in the User-Agent of every request.
            Required; printable ASCII.
        token: API token. The default is the token built into the SDK.
        base_url: The server, as scheme and host.
        timeout: Seconds to wait for a response.
        max_concurrent: How many requests may be in flight at once. It does
            not raise the server's rate limit.
        max_retries: How many times a request is retried after a 429, 502,
            503, 504 or connection failure. 0 disables retries.
        max_wait: The longest pause, in seconds, the client sleeps through
            before a retry. When the server asks for a longer wait, the call
            raises :class:`RateLimitError` instead.
        http_client: An ``httpx.Client`` to send requests with. The caller
            closes it; ``timeout`` is then ignored.

    Raises:
        ValueError: An argument is invalid.
    """

    def __init__(
        self,
        app: str,
        *,
        token: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 60.0,
        max_concurrent: int = 1,
        max_retries: int = 4,
        max_wait: float = 30.0,
        http_client: httpx.Client | None = None,
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
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout)
        self._max_retries = max_retries
        self._max_wait = max_wait
        self._slots = threading.BoundedSemaphore(max_concurrent)

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
        self._sleep: Callable[[float], None] = time.sleep
        self._jitter: Callable[[], float] = random.random

    def close(self) -> None:
        """Release the client's connections."""
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def rate_limits(self) -> tuple[RateLimit, ...]:
        """The rate limits reported by the most recent response."""
        with self._lock:
            return self._limits

    def query(self, query: str, *, page_size: int | None = None, cursor: int | None = None) -> Page:
        """Run a query and return one page of results.

        ``Page.has_more`` says whether there are records after this page,
        and :meth:`next_page` fetches them.

        Args:
            query: A domain (``example.com``), a wildcard
                (``*.example.com``), an address, a network in CIDR notation
                or an AS number (``AS64496``).
            page_size: The most records to return. The default is 100. The
                token's policy caps it, without an error: a larger request
                returns the token's maximum.
            cursor: Offset of the first record. To continue from a page,
                :meth:`next_page` is simpler.

        Raises:
            RateLimitError: A rate limit is reached and was not waited out.
            ForbiddenError: The token does not allow the query.
            UnauthorizedError: The token is not accepted.
            APIError: Any other error response.
            TransportError: The request got no answer.
        """
        return self._search({"q": query}, page_size, cursor)

    def query_combined(
        self,
        *,
        domain: str | None = None,
        ip: str | None = None,
        asn: int | None = None,
        page_size: int | None = None,
        cursor: int | None = None,
    ) -> Page:
        """Run a query for records that match every argument given.

        The token must allow combined queries.

        Args:
            domain: A domain, or a leading wildcard such as
                ``*.example.com``.
            ip: An address, or a network in CIDR notation.
            asn: An AS number.
            page_size: The most records to return, as for :meth:`query`.
            cursor: Offset of the first record, as for :meth:`query`.

        Raises:
            ValueError: None of ``domain``, ``ip`` and ``asn`` is given.

        Otherwise raises as :meth:`query` does.
        """
        params = {}
        if domain:
            params["domain"] = domain
        if ip:
            params["ip"] = ip
        if asn:
            params["asn"] = f"AS{asn}"
        if not params:
            raise ValueError("a combined query needs at least one of domain, ip and asn")
        return self._search(params, page_size, cursor)

    def next_page(self, page: Page) -> Page | None:
        """Return the page after ``page``, or ``None`` if it is the last one.

        The same query is continued where ``page`` ended. The client never
        does this by itself: every page is a request against the rate limit.

        Raises:
            ForbiddenError: The token may not page that deep.
            ValueError: ``page`` was not returned by this package.

        Otherwise raises as :meth:`query` does.
        """
        if page._request is None:
            raise ValueError("next_page needs a page returned by this package")
        if not page.has_more:
            return None
        return self._search(dict(page._request), None, page.next_cursor)

    def _search(self, params: dict[str, Any], page_size: int | None, cursor: int | None) -> Page:
        if page_size is not None:
            params["limit"] = page_size  # the API's name for the page size
        if cursor is not None:
            params["cursor"] = cursor
        return page_from_dict(self._get(params), request=params)

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        """Send the request, waiting and retrying as the class promises."""
        with self._slots:
            attempt = 0
            while True:
                self._ready()
                try:
                    return self._send(params)
                except _Retry as retry:
                    if attempt >= self._max_retries or retry.after > self._max_wait:
                        self._give_up(retry.error)
                        raise retry.error from retry.__cause__
                    wait = min(self._backoff(attempt), self._max_wait)
                    if retry.after > 0:
                        # up to a second later than asked, so that the clients
                        # sharing a limit do not all come back at the same instant
                        wait = max(wait, retry.after + self._jitter())
                    self._pause(wait)
                    attempt += 1

    def _ready(self) -> None:
        """Block until the client may send, or raise the reason it will not."""
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
            wait = (self._not_before - now).total_seconds() if self._not_before else 0.0
        if wait > 0:
            self._sleep(wait)

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

    def _send(self, params: dict[str, Any]) -> dict[str, Any]:
        """Make one attempt. Raises _Retry when trying again can help."""
        try:
            response = self._http.get(self._url, params=params, headers=self._headers)
        except httpx.TimeoutException as exc:
            # A request that timed out was probably an expensive search still
            # running on the server: sending it again would double the load.
            raise TransportError(f"request timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise _Retry(TransportError(str(exc) or type(exc).__name__)) from exc

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
