from __future__ import annotations

import threading
import time
from collections.abc import Callable
from types import TracebackType
from typing import Any

import httpx

from ._base import DEFAULT_BASE_URL, _BaseClient, _Retry
from ._models import Page, page_from_dict


class Client(_BaseClient):
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
        super().__init__(
            app,
            token=token,
            base_url=base_url,
            max_concurrent=max_concurrent,
            max_retries=max_retries,
            max_wait=max_wait,
        )
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout)
        self._slots = threading.BoundedSemaphore(max_concurrent)
        # replaced in tests
        self._sleep: Callable[[float], None] = time.sleep

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
        return self._get(self._paged({"q": query}, page_size, cursor))

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
        return self._get(self._paged(self._combined(domain, ip, asn), page_size, cursor))

    def next_page(self, page: Page) -> Page | None:
        """Return the page after ``page``, or ``None`` if it is the last one.

        The same query is continued where ``page`` ended. The client never
        does this by itself: every page is a request against the rate limit.

        Raises:
            ForbiddenError: The token may not page that deep.
            ValueError: ``page`` was not returned by this package.

        Otherwise raises as :meth:`query` does.
        """
        request = self._after(page)
        return None if request is None else self._get(request)

    def _get(self, params: dict[str, Any]) -> Page:
        """Send the request, waiting and retrying as the class promises."""
        with self._slots:
            attempt = 0
            while True:
                wait = self._ready()
                if wait > 0:
                    self._sleep(wait)
                try:
                    return page_from_dict(self._send(params), request=params)
                except _Retry as retry:
                    self._retry_later(retry, attempt)
                    attempt += 1

    def _send(self, params: dict[str, Any]) -> dict[str, Any]:
        """Make one attempt. Raises _Retry when trying again can help."""
        try:
            response = self._http.get(self._url, params=params, headers=self._headers)
        except httpx.TransportError as exc:
            raise self._unanswered(exc) from exc
        return self._read(response)
