from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from conftest import PAGE, Handler, Harness, answer

import activedns
from activedns import (
    APIError,
    Client,
    ForbiddenError,
    RateLimitError,
    TransportError,
    UnauthorizedError,
)
from activedns._token import EMBEDDED_TOKEN


def test_arguments_are_checked() -> None:
    for args, kwargs in [
        (("",), {}),
        (("  ",), {}),
        (("my\ntool",), {}),
        (("t",), {"token": " "}),
        (("t",), {"base_url": "activedns.net"}),
        (("t",), {"base_url": "ftp://activedns.net"}),
        (("t",), {"max_concurrent": 0}),
        (("t",), {"max_retries": -1}),
        (("t",), {"max_wait": -1}),
    ]:
        with pytest.raises(ValueError):
            Client(*args, **kwargs)


def test_the_sdk_carries_a_token() -> None:
    with Client("mytool/1.2") as client:
        assert EMBEDDED_TOKEN and client._headers["Authorization"] == f"Bearer {EMBEDDED_TOKEN}"
        assert client._url == "https://activedns.net/api/v2/query"


def test_query_sends_and_decodes() -> None:
    h = Harness(lambda r: answer())
    page = h.client.query("*.example.com", page_size=50, cursor=100)

    request = h.requests[0]
    assert request.method == "GET" and request.url.path == "/api/v2/query"
    assert dict(request.url.params) == {"q": "*.example.com", "limit": "50", "cursor": "100"}
    assert request.headers["Authorization"] == "Bearer test-token"
    assert request.headers["User-Agent"].startswith(
        f"mytool/1.2 activedns-py/{activedns.__version__} (python "
    )

    assert len(page.records) == 2 and page.count == 4321 and page.count_estimated and not page.count_capped
    assert page.next_cursor == 2 and page.has_more and not page.timed_out
    assert page.query == "*.example.com" and page.query_type == "wildcard_domain"

    first, second = page.records
    assert (first.id, first.ip_address, first.domain) == (918273645, "192.0.2.10", "cdn.example.net")
    assert (first.asn, first.country, first.ip_version) == (64500, "NO", 4)
    assert first.observed == datetime(2026, 10, 4, 22, 15, 3, tzinfo=timezone.utc)
    assert first.first_seen == datetime(2025, 3, 11, 8, 40, 12, tzinfo=timezone.utc)
    assert first.aliases[0].name == "www.example.com" and len(first.aliases[0].chain) == 2
    assert second.asn is None and second.country is None
    assert second.known_as_total == 1000 and second.known_as[0].name == "a.example.org"


def test_query_combined() -> None:
    h = Harness(lambda r: answer())
    h.client.query_combined(domain="*.example.com", asn=13335)
    assert dict(h.requests[0].url.params) == {"domain": "*.example.com", "asn": "AS13335"}
    with pytest.raises(ValueError):
        h.client.query_combined()
    assert h.hits == 1


def pages(total: int) -> Handler:
    """A handler serving a search of `total` records in pages, like the API."""

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = int(request.url.params.get("cursor", 0))
        limit = int(request.url.params.get("limit", 100))
        records = [dict(PAGE["records"][0], id=i) for i in range(cursor, min(cursor + limit, total))]
        end = cursor + len(records)
        return answer(body={"records": records, "count": total, "next_cursor": end, "has_more": end < total})

    return handler


def test_next_page_continues_the_query() -> None:
    h = Harness(pages(25))
    page = h.client.query("example.com", page_size=10)
    assert (len(page.records), page.count, page.has_more) == (10, 25, True)
    assert h.hits == 1  # nothing else is fetched unasked

    ids = []
    while page is not None:
        ids += [r.id for r in page.records]
        page = h.client.next_page(page)
    assert ids == list(range(25))
    assert h.hits == 3
    assert [dict(r.url.params) for r in h.requests[1:]] == [
        {"q": "example.com", "limit": "10", "cursor": "10"},
        {"q": "example.com", "limit": "10", "cursor": "20"},
    ]


def test_next_page_repeats_a_combined_query() -> None:
    h = Harness(lambda r: answer())
    page = h.client.query_combined(domain="*.example.com", asn=13335, page_size=2)
    h.client.next_page(page)
    assert dict(h.requests[0].url.params) == {"domain": "*.example.com", "asn": "AS13335", "limit": "2"}
    assert dict(h.requests[1].url.params) == {
        "domain": "*.example.com",
        "asn": "AS13335",
        "limit": "2",
        "cursor": "2",
    }


def test_next_page_needs_a_page_of_this_package() -> None:
    h = Harness(lambda r: answer())
    made_up = activedns.Page(records=(), count=0, next_cursor=0, has_more=True, query="", query_type="")
    with pytest.raises(ValueError):
        h.client.next_page(made_up)
    assert h.hits == 0


def test_next_page_beyond_the_tokens_depth() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "cursor" in request.url.params:
            return answer(403, {"error": "cursor beyond your token's limit of 100", "code": 403})
        return answer()

    h = Harness(handler)
    page = h.client.query("*.example.com")
    with pytest.raises(ForbiddenError):
        h.client.next_page(page)


def test_a_timed_out_page_is_returned_as_it_is() -> None:
    h = Harness(lambda r: answer(body=dict(PAGE, timed_out=True)))
    page = h.client.query("*.example.com")
    assert page.timed_out and len(page.records) == 2 and h.hits == 1


def test_errors_that_retrying_cannot_fix_are_not_retried() -> None:
    for status in (400, 404, 500):
        h = Harness(lambda r, status=status: answer(status, {"error": "no", "code": status}))
        with pytest.raises(APIError) as caught:
            h.client.query("example.com")
        assert caught.value.status_code == status and caught.value.message == "no"
        assert h.hits == 1 and h.sleeps == []


def test_server_trouble_is_retried_with_backoff() -> None:
    h = Harness(lambda r: answer(503, {"error": "busy"}))
    with pytest.raises(APIError) as caught:
        h.client.query("example.com")
    assert caught.value.status_code == 503
    assert h.hits == 5
    # 1, 2, 4, 8 seconds, halved by the jitter of the test (0)
    assert h.sleeps == [0.5, 1.0, 2.0, 4.0]


def test_a_retry_succeeds() -> None:
    h = Harness(lambda r: answer(502, {"error": "bad gateway"}) if len(h.requests) < 3 else answer())
    assert h.client.query("example.com").count == 4321
    assert h.hits == 3


def test_backoff_is_capped() -> None:
    h = Harness(lambda r: answer())
    h.client._jitter = lambda: 0.999
    assert 0.9 < h.client._backoff(0) <= 1.0
    assert 29 < h.client._backoff(20) <= 30
    assert 29 < h.client._backoff(5000) <= 30


def test_retry_after_is_honoured() -> None:
    h = Harness(
        lambda r: answer(429, {"error": "slow down"}, Retry_After="7") if len(h.requests) == 1 else answer()
    )
    h.client.query("example.com")
    assert h.hits == 2 and h.sleeps == [7.0]


def test_a_distant_limit_stops_the_client() -> None:
    h = Harness(
        lambda r: answer(
            429, {"error": "rate limit reached", "code": 429, "exceeded": "token"}, Retry_After="1800"
        )
    )
    start = h.clock
    with pytest.raises(RateLimitError) as caught:
        h.client.query("example.com")
    limit = caught.value
    assert limit.exceeded == "token" and not limit.local and limit.retry_at == start + timedelta(minutes=30)
    assert h.hits == 1 and h.sleeps == []

    # further calls do not reach the server
    for _ in range(5):
        with pytest.raises(RateLimitError) as caught:
            h.client.query("example.org")
        assert caught.value.local and caught.value.exceeded == "token"
    assert h.hits == 1

    # once it has reset, the client sends again
    h.advance(31 * 60)
    with pytest.raises(RateLimitError):
        h.client.query("example.com")
    assert h.hits == 2


def test_the_apis_rate_limit_answers() -> None:
    """The burst is spent, the next request is refused for two seconds, and the client waits them out."""
    policy = '"address";q=60;w=120, "network";q=300;w=120'

    def handler(request: httpx.Request) -> httpx.Response:
        if len(h.requests) == 2:
            body = {"error": "rate limit reached for your address", "code": 429, "exceeded": "address"}
            return answer(429, body, RateLimit_Policy=policy, RateLimit='"address";r=0;t=2', Retry_After="2")
        return answer(RateLimit_Policy=policy, RateLimit='"address";r=41;t=2')

    h = Harness(handler)
    h.client.query("example.com")
    (limit,) = h.client.rate_limits
    assert (limit.name, limit.limit, limit.remaining) == ("address", 60, 41)
    assert limit.reset == h.clock + timedelta(seconds=2)

    h.client.query("example.com")
    assert h.hits == 3
    assert 2 <= sum(h.sleeps) <= 4


def test_an_exhausted_limit_delays_the_next_request() -> None:
    h = Harness(lambda r: answer(RateLimit='"address";r=0;t=2'))
    h.client.query("example.com")
    assert h.sleeps == []
    h.client.query("example.com")
    assert h.sleeps == [2.0] and h.hits == 2


def test_requests_left_do_not_delay() -> None:
    h = Harness(lambda r: answer(RateLimit='"address";r=3;t=2'))
    h.client.query("example.com")
    h.client.query("example.com")
    assert h.sleeps == []


def test_a_429_without_retry_after_uses_the_limits_reset() -> None:
    h = Harness(
        lambda r: (
            answer(429, {"error": "slow down"}, RateLimit='"token";r=0;t=5')
            if len(h.requests) == 1
            else answer()
        )
    )
    h.client.query("example.com")
    assert h.hits == 2 and sum(h.sleeps) == 5.0


def test_a_refused_token_stops_the_client() -> None:
    h = Harness(lambda r: answer(401, {"error": "missing or unknown token", "code": 401}))
    for _ in range(3):
        with pytest.raises(UnauthorizedError):
            h.client.query("example.com")
    assert h.hits == 1


def test_a_forbidden_query_does_not_stop_the_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["q"] == "AS64496":
            return answer(403, {"error": "query type asn is not allowed for your token", "code": 403})
        return answer()

    h = Harness(handler)
    with pytest.raises(ForbiddenError) as caught:
        h.client.query("AS64496")
    assert not isinstance(caught.value, UnauthorizedError) and "asn" in caught.value.message
    h.client.query("example.com")
    assert h.hits == 2 and h.sleeps == []


def test_connection_failures_are_retried_and_timeouts_are_not() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    h = Harness(refuse, max_retries=2)
    with pytest.raises(TransportError):
        h.client.query("example.com")
    assert h.hits == 3

    def time_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    h = Harness(time_out)
    with pytest.raises(TransportError):
        h.client.query("example.com")
    assert h.hits == 1 and h.sleeps == []


def test_retries_can_be_disabled() -> None:
    h = Harness(lambda r: answer(503, {"error": "busy"}), max_retries=0)
    with pytest.raises(APIError):
        h.client.query("example.com")
    assert h.hits == 1


def test_an_answer_that_is_not_json() -> None:
    h = Harness(lambda r: httpx.Response(200, content=b"<html>"))
    with pytest.raises(activedns.ActiveDNSError):
        h.client.query("example.com")
    h = Harness(lambda r: httpx.Response(500, content=b"<html>"))
    with pytest.raises(APIError) as caught:
        h.client.query("example.com")
    assert caught.value.message == "Internal Server Error"
