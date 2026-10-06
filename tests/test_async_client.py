"""The asyncio client. What it shares with Client is tested in test_client.py."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest
from conftest import PAGE, AsyncHarness, answer

import activedns
from activedns import (
    APIError,
    AsyncClient,
    ForbiddenError,
    RateLimitError,
    TransportError,
    UnauthorizedError,
)
from activedns._token import EMBEDDED_TOKEN


def test_arguments_are_checked() -> None:
    for args, kwargs in [
        (("",), {}),
        (("t",), {"token": " "}),
        (("t",), {"base_url": "activedns.net"}),
        (("t",), {"max_concurrent": 0}),
        (("t",), {"max_retries": -1}),
    ]:
        with pytest.raises(ValueError):
            AsyncClient(*args, **kwargs)


def test_the_sdk_carries_a_token() -> None:
    async def run() -> None:
        async with AsyncClient("mytool/1.2") as client:
            assert client._headers["Authorization"] == f"Bearer {EMBEDDED_TOKEN}"
            assert client._url == "https://activedns.net/api/v2/query"

    asyncio.run(run())


def test_query_sends_and_decodes() -> None:
    h = AsyncHarness(lambda r: answer())
    page = asyncio.run(h.client.query("*.example.com", page_size=50, cursor=100))

    request = h.requests[0]
    assert request.method == "GET" and request.url.path == "/api/v2/query"
    assert dict(request.url.params) == {"q": "*.example.com", "limit": "50", "cursor": "100"}
    assert request.headers["Authorization"] == "Bearer test-token"
    assert request.headers["User-Agent"].startswith(
        f"mytool/1.2 activedns-py/{activedns.__version__} (python "
    )
    assert len(page.records) == 2 and page.count == 4321 and page.has_more
    assert page.records[0].domain == "cdn.example.net"


def test_query_combined() -> None:
    async def run() -> None:
        await h.client.query_combined(domain="*.example.com", asn=13335)
        with pytest.raises(ValueError):
            await h.client.query_combined()

    h = AsyncHarness(lambda r: answer())
    asyncio.run(run())
    assert h.hits == 1
    assert dict(h.requests[0].url.params) == {"domain": "*.example.com", "asn": "AS13335"}


def test_next_page_continues_the_query() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        cursor = int(request.url.params.get("cursor", 0))
        records = [dict(PAGE["records"][0], id=i) for i in range(cursor, min(cursor + 10, 25))]
        end = cursor + len(records)
        return answer(body={"records": records, "count": 25, "next_cursor": end, "has_more": end < 25})

    async def run() -> list[int]:
        ids = []
        page = await h.client.query("example.com", page_size=10)
        assert h.hits == 1  # nothing else is fetched unasked
        while page is not None:
            ids += [r.id for r in page.records]
            page = await h.client.next_page(page)
        return ids

    h = AsyncHarness(handler)
    assert asyncio.run(run()) == list(range(25))
    assert [dict(r.url.params) for r in h.requests[1:]] == [
        {"q": "example.com", "limit": "10", "cursor": "10"},
        {"q": "example.com", "limit": "10", "cursor": "20"},
    ]


def test_next_page_needs_a_page_of_this_package() -> None:
    h = AsyncHarness(lambda r: answer())
    made_up = activedns.Page(records=(), count=0, next_cursor=0, has_more=True, query="", query_type="")
    with pytest.raises(ValueError):
        asyncio.run(h.client.next_page(made_up))
    assert h.hits == 0


def test_server_trouble_is_retried_with_backoff() -> None:
    h = AsyncHarness(lambda r: answer(503, {"error": "busy"}))
    with pytest.raises(APIError) as caught:
        asyncio.run(h.client.query("example.com"))
    assert caught.value.status_code == 503
    assert h.hits == 5 and h.sleeps == [0.5, 1.0, 2.0, 4.0]


def test_retry_after_is_honoured() -> None:
    h = AsyncHarness(
        lambda r: answer(429, {"error": "slow down"}, Retry_After="7") if len(h.requests) == 1 else answer()
    )
    asyncio.run(h.client.query("example.com"))
    assert h.hits == 2 and h.sleeps == [7.0]


def test_a_distant_limit_stops_the_client() -> None:
    async def run() -> None:
        with pytest.raises(RateLimitError) as caught:
            await h.client.query("example.com")
        assert caught.value.exceeded == "token" and not caught.value.local
        assert caught.value.retry_at == h.clock + timedelta(minutes=30)
        with pytest.raises(RateLimitError) as caught:
            await h.client.query("example.org")
        assert caught.value.local

    h = AsyncHarness(
        lambda r: answer(429, {"error": "rate limit reached", "exceeded": "token"}, Retry_After="1800")
    )
    asyncio.run(run())
    assert h.hits == 1 and h.sleeps == []


def test_an_exhausted_limit_delays_the_next_request() -> None:
    async def run() -> None:
        await h.client.query("example.com")
        assert h.sleeps == []
        await h.client.query("example.com")

    h = AsyncHarness(lambda r: answer(RateLimit='"address";r=0;t=2'))
    asyncio.run(run())
    assert h.sleeps == [2.0] and h.hits == 2
    assert h.client.rate_limits[0].name == "address"


def test_a_refused_token_stops_the_client() -> None:
    async def run() -> None:
        for _ in range(3):
            with pytest.raises(UnauthorizedError):
                await h.client.query("example.com")

    h = AsyncHarness(lambda r: answer(401, {"error": "missing or unknown token", "code": 401}))
    asyncio.run(run())
    assert h.hits == 1


def test_a_forbidden_query_does_not_stop_the_client() -> None:
    async def run() -> None:
        with pytest.raises(ForbiddenError):
            await h.client.query("AS64496")
        await h.client.query("example.com")

    h = AsyncHarness(lambda r: answer(403, {"error": "no"}) if r.url.params["q"] == "AS64496" else answer())
    asyncio.run(run())
    assert h.hits == 2


def test_connection_failures_are_retried_and_timeouts_are_not() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    h = AsyncHarness(refuse, max_retries=2)
    with pytest.raises(TransportError):
        asyncio.run(h.client.query("example.com"))
    assert h.hits == 3

    def time_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    h = AsyncHarness(time_out)
    with pytest.raises(TransportError):
        asyncio.run(h.client.query("example.com"))
    assert h.hits == 1 and h.sleeps == []


def in_flight(max_concurrent: int, tasks: int) -> int:
    """The most requests in flight at once when `tasks` tasks query through one client."""
    now = most = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal now, most
        now += 1
        most = max(most, now)
        await asyncio.sleep(0.01)
        now -= 1
        return answer()

    async def run() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with AsyncClient("mytool/1.2", http_client=http, max_concurrent=max_concurrent) as client:
            pages = await asyncio.gather(*(client.query(f"{i}.example.com") for i in range(tasks)))
        assert len(pages) == tasks
        await http.aclose()

    asyncio.run(run())
    return most


def test_tasks_share_the_clients_slots() -> None:
    assert in_flight(max_concurrent=1, tasks=6) == 1
    assert in_flight(max_concurrent=3, tasks=6) == 3


def test_a_slow_down_holds_every_task_back() -> None:
    """One task is told to wait; the others do not send in the meantime."""

    def handler(request: httpx.Request) -> httpx.Response:
        return answer(429, {"error": "slow down"}, Retry_After="7") if len(h.requests) == 1 else answer()

    async def run() -> None:
        await asyncio.gather(*(h.client.query(f"{i}.example.com") for i in range(3)))

    h = AsyncHarness(handler, max_concurrent=3)
    start = h.clock
    asyncio.run(run())
    assert h.hits == 4
    assert h.clock >= start + timedelta(seconds=7)


def test_closing() -> None:
    async def run() -> None:
        async with AsyncClient("mytool/1.2") as client:
            own = client._http
        assert own.is_closed

        given = httpx.AsyncClient()
        async with AsyncClient("mytool/1.2", http_client=given):
            pass
        assert not given.is_closed  # the caller closes it
        await given.aclose()

    asyncio.run(run())
