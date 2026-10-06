"""End-to-end tests: AsyncClient against the live API, with the token built into the SDK.

The same checks as test_live.py makes of Client, and tasks that share a client.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from test_live import APP, check_records

from activedns import APIError, AsyncClient, ForbiddenError, UnauthorizedError


def live(test: Callable[[AsyncClient], Awaitable[None]]) -> None:
    """Run a test with a client of its own."""

    async def main() -> None:
        async with AsyncClient(APP) as client:
            await test(client)

    asyncio.run(main())


def test_domain() -> None:
    async def test(client: AsyncClient) -> None:
        page = await client.query("example.com")
        assert (page.query, page.query_type) == ("example.com", "domain")
        check_records(page)
        assert {r.domain for r in page.records} == {"example.com"}

        limits = {limit.name: limit for limit in client.rate_limits}
        assert "address" in limits
        address = limits["address"]
        assert address.limit is not None and 0 <= address.remaining <= address.limit

    live(test)


def test_paging() -> None:
    async def test(client: AsyncClient) -> None:
        first = await client.query("*.example.com", page_size=1)
        check_records(first)
        assert first.query_type == "wildcard_domain"
        assert (len(first.records), first.has_more, first.next_cursor) == (1, True, 1)

        second = await client.next_page(first)
        assert second is not None
        check_records(second)
        assert (len(second.records), second.next_cursor) == (1, 2)
        assert second.records[0].id != first.records[0].id

    live(test)


def test_tasks_share_a_client() -> None:
    async def test(client: AsyncClient) -> None:
        address, network, domain = await asyncio.gather(
            client.query("1.1.1.1", page_size=2),
            client.query("1.1.1.0/24", page_size=2),
            client.query("*.example.com", page_size=1),
        )
        for page in (address, network, domain):
            check_records(page)
        # every task got the answer to its own question
        assert (address.query_type, network.query_type, domain.query_type) == (
            "ip",
            "cidr",
            "wildcard_domain",
        )
        assert {r.ip_address for r in address.records} == {"1.1.1.1"}
        assert all(r.ip_address.startswith("1.1.1.") for r in network.records)

    live(test)


def test_forbidden() -> None:
    """What the built-in token may not do is refused with a 403, and the client goes on working."""

    async def test(client: AsyncClient) -> None:
        with pytest.raises(ForbiddenError):
            await client.query("AS13335")
        with pytest.raises(ForbiddenError):
            await client.query_combined(domain="*.example.com", asn=13335)
        assert (await client.query("example.com")).records

    live(test)


def test_bad_query() -> None:
    async def test(client: AsyncClient) -> None:
        with pytest.raises(APIError) as caught:
            await client.query("not a query")
        assert caught.value.status_code == 400 and caught.value.message

    live(test)


def test_unknown_token() -> None:
    async def main() -> None:
        async with AsyncClient(APP, token="00000000-0000-4000-8000-000000000000") as stranger:
            with pytest.raises(UnauthorizedError):
                await stranger.query("example.com")

    asyncio.run(main())
