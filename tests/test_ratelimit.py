from __future__ import annotations

from datetime import datetime, timedelta, timezone

from activedns import RateLimit
from activedns._client import Client
from activedns._models import parse_time
from activedns._ratelimit import exhausted_until, parse_rate_limits

NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)


def after(seconds: int) -> datetime:
    return NOW + timedelta(seconds=seconds)


def test_parse_rate_limits() -> None:
    cases = [
        (
            {
                "RateLimit": '"address";r=41;t=2',
                "RateLimit-Policy": '"address";q=60;w=120, "network";q=300;w=120',
            },
            (RateLimit(name="address", remaining=41, limit=60, reset=after(2)),),
        ),
        (
            {"RateLimit": '"a";r=0;t=1, "b";r=9;t=30', "RateLimit-Policy": '"b";q=10;w=60'},
            (
                RateLimit(name="a", remaining=0, limit=None, reset=after(1)),
                RateLimit(name="b", remaining=9, limit=10, reset=after(30)),
            ),
        ),
        ({"RateLimit": '"a, b";r=1'}, (RateLimit(name="a, b", remaining=1),)),
        ({"RateLimit": ";;;"}, ()),
        ({"RateLimit": '"a";t=3'}, ()),
        ({}, ()),
    ]
    for headers, want in cases:
        assert parse_rate_limits(headers, NOW) == want


def test_exhausted_until() -> None:
    limits = (
        RateLimit(name="address", remaining=0, reset=after(2)),
        RateLimit(name="network", remaining=0, reset=after(9)),
        RateLimit(name="token", remaining=5, reset=after(60)),
    )
    assert exhausted_until(limits, NOW) == (after(9), "network")
    assert exhausted_until(limits[2:], NOW) is None
    assert exhausted_until((RateLimit(name="old", remaining=0, reset=after(-1)),), NOW) is None


def test_retry_after() -> None:
    assert Client._retry_after("7", NOW) == 7.0
    assert Client._retry_after("-3", NOW) == 0.0
    assert Client._retry_after("Mon, 05 Oct 2026 12:00:30 GMT", NOW) == 30.0
    assert Client._retry_after("soon", NOW) == 0.0
    assert Client._retry_after(None, NOW) == 0.0


def test_parse_time() -> None:
    whole = datetime(2026, 10, 4, 22, 15, 3, tzinfo=timezone.utc)
    assert parse_time("2026-10-04T22:15:03Z") == whole
    assert parse_time("2026-10-04T22:15:03.5Z") == whole.replace(microsecond=500000)
    assert parse_time("2026-10-04T22:15:03.123456789Z") == whole.replace(microsecond=123456)
    assert parse_time("2026-10-05T00:15:03+02:00") == whole
