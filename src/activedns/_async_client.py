from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any

import httpx

from ._base import DEFAULT_BASE_URL, _BaseClient, _Retry
from ._models import Page, page_from_dict


class AsyncClient(_BaseClient):
    """An ActiveDNS API client for asyncio: :class:`Client` with awaitable methods.

    It queries, waits, retries and raises as :class:`Client` does. Use one
    client per program, from one event loop: request pacing and rate-limit
    state are kept per client. Close it with :meth:`aclose` when done, or use
    it with ``async with``.

    Args:
        app: Name and version of the calling program, such as
            ``"mytool/1.2"``. Sent in the User-Agent of every request.
            Required; printable ASCII.
        token: API token. The default is the token built into the SDK.
        base_url: The server, as scheme and host.
        timeout: Seconds to wait for a response.
        max_concurrent: How many requests may be in flight at once, however
            many tasks are querying. It does not raise the server's rate
            limit.
        max_retries: How many times a request is retried after a 429, 502,
            503, 504 or connection failure. 0 disables retries.
        max_wait: The longest pause, in seconds, the client sleeps through
            before a retry. When the server asks for a longer wait, the call
            raises :class:`RateLimitError` instead.
        http_client: An ``httpx.AsyncClient`` to send requests with. The
            caller closes it; ``timeout`` is then ignored.

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
        http_client: httpx.AsyncClient | None = None,
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
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self._slots = asyncio.Semaphore(max_concurrent)
        # replaced in tests
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    async def aclose(self) -> None:
        """Release the client's connections."""
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> AsyncClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def query(self, query: str, *, page_size: int | None = None, cursor: int | None = None) -> Page:
        """Run a query and return one page of results.

        Arguments and errors are those of :meth:`Client.query`.
        """
        return await self._get(self._paged({"q": query}, page_size, cursor))

    async def query_combined(
        self,
        *,
        domain: str | None = None,
        ip: str | None = None,
        asn: int | None = None,
        page_size: int | None = None,
        cursor: int | None = None,
    ) -> Page:
        """Run a query for records that match every argument given.

        Arguments and errors are those of :meth:`Client.query_combined`.
        """
        return await self._get(self._paged(self._combined(domain, ip, asn), page_size, cursor))

    async def next_page(self, page: Page) -> Page | None:
        """Return the page after ``page``, or ``None`` if it is the last one.

        The client never does this by itself: every page is a request
        against the rate limit. Errors are those of :meth:`Client.next_page`.
        """
        request = self._after(page)
        return None if request is None else await self._get(request)

    async def _get(self, params: dict[str, Any]) -> Page:
        """Send the request, waiting and retrying as the class promises."""
        async with self._slots:
            attempt = 0
            while True:
                wait = self._ready()
                if wait > 0:
                    await self._sleep(wait)
                try:
                    return page_from_dict(await self._send(params), request=params)
                except _Retry as retry:
                    self._retry_later(retry, attempt)
                    attempt += 1

    async def _send(self, params: dict[str, Any]) -> dict[str, Any]:
        """Make one attempt. Raises _Retry when trying again can help."""
        try:
            response = await self._http.get(self._url, params=params, headers=self._headers)
        except httpx.TransportError as exc:
            raise self._unanswered(exc) from exc
        return self._read(response)
