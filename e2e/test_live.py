"""End-to-end tests: Client against the live API, with the token built into the SDK.

    pytest e2e

The files of this directory make about thirty requests together, and test the
installed package: this one Client, test_live_async.py AsyncClient, and
test_examples.py the programs in examples/.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest

from activedns import APIError, Client, ForbiddenError, Page, UnauthorizedError

# the name tells the operators of the API that these are the SDK's tests
APP = "activedns-py-e2e/1.0"


@pytest.fixture(scope="module")
def client() -> Iterator[Client]:
    with Client(APP) as client:
        yield client


def check_records(page: Page) -> None:
    """Fail for a record that cannot be right."""
    assert page.records, f"{page.query}: no records"
    assert page.count >= len(page.records)
    soon = datetime.now(timezone.utc) + timedelta(hours=1)
    for r in page.records:
        assert r.id and r.domain and r.ip_address
        assert r.ip_version == (6 if ":" in r.ip_address else 4)
        assert r.first_seen.year >= 2020 and r.first_seen <= r.observed <= soon


def test_domain(client: Client) -> None:
    page = client.query("example.com")
    assert (page.query, page.query_type) == ("example.com", "domain")
    check_records(page)
    assert {r.domain for r in page.records} == {"example.com"}

    # the server reports the limit of the built-in token: per address
    limits = {limit.name: limit for limit in client.rate_limits}
    assert "address" in limits
    address = limits["address"]
    assert address.limit is not None and 0 <= address.remaining <= address.limit


def test_paging(client: Client) -> None:
    first = client.query("*.example.com", page_size=1)
    check_records(first)
    assert first.query_type == "wildcard_domain"
    assert (len(first.records), first.has_more, first.next_cursor) == (1, True, 1)

    second = client.next_page(first)
    assert second is not None
    check_records(second)
    assert (len(second.records), second.next_cursor) == (1, 2)
    assert second.records[0].id != first.records[0].id


def test_address_and_network(client: Client) -> None:
    page = client.query("1.1.1.1", page_size=2)
    check_records(page)
    assert (page.query_type, len(page.records), page.has_more) == ("ip", 2, True)
    assert {r.ip_address for r in page.records} == {"1.1.1.1"}

    page = client.query("1.1.1.0/24", page_size=2)
    check_records(page)
    assert page.query_type == "cidr"
    assert all(r.ip_address.startswith("1.1.1.") for r in page.records)


def test_forbidden(client: Client) -> None:
    """What the built-in token may not do is refused with a 403, and the client goes on working."""
    with pytest.raises(ForbiddenError):
        client.query("AS13335")
    with pytest.raises(ForbiddenError):
        client.query_combined(domain="*.example.com", asn=13335)
    with pytest.raises(ForbiddenError):
        client.query("1.0.0.0/8")
    with pytest.raises(ForbiddenError):
        client.query("*.example.com", cursor=100000)
    assert client.query("example.com").records


def test_bad_query(client: Client) -> None:
    with pytest.raises(APIError) as caught:
        client.query("not a query")
    assert caught.value.status_code == 400 and caught.value.message


def test_unknown_token() -> None:
    with Client(APP, token="00000000-0000-4000-8000-000000000000") as stranger:
        with pytest.raises(UnauthorizedError):
            stranger.query("example.com")
