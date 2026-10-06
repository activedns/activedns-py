from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from activedns import AsyncClient, Client

PAGE = {
    "records": [
        {
            "id": 918273645,
            "ip_address": "192.0.2.10",
            "domain": "cdn.example.net",
            "observed": "2026-10-04T22:15:03Z",
            "first_seen": "2025-03-11T08:40:12Z",
            "ip_version": 4,
            "asn": 64500,
            "country": "NO",
            "aliases": [{"name": "www.example.com", "chain": ["www.example.com", "cdn.example.net"]}],
        },
        {
            "id": 2,
            "ip_address": "2001:db8::1",
            "domain": "example.org",
            "observed": "2026-10-01T00:00:00Z",
            "first_seen": "2026-10-01T00:00:00Z",
            "ip_version": 6,
            "known_as": [{"name": "a.example.org", "chain": ["a.example.org", "example.org"]}],
            "known_as_total": 1000,
        },
    ],
    "count": 4321,
    "count_estimated": True,
    "next_cursor": 2,
    "has_more": True,
    "query": "*.example.com",
    "query_type": "wildcard_domain",
}

Handler = Callable[[httpx.Request], httpx.Response]


def answer(status: int = 200, body: Any = None, **headers: str) -> httpx.Response:
    """A JSON response; header names are given with underscores."""
    return httpx.Response(
        status,
        content=json.dumps(PAGE if body is None else body),
        headers={"Content-Type": "application/json", **{k.replace("_", "-"): v for k, v in headers.items()}},
    )


class Harness:
    """A client against a fake server, with a clock that only sleeping moves."""

    def __init__(self, handler: Handler, **options: Any) -> None:
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.clock = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)

        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        options.setdefault("token", "test-token")
        self.client = Client(
            "mytool/1.2", http_client=httpx.Client(transport=httpx.MockTransport(handle)), **options
        )
        self.client._now = lambda: self.clock
        self.client._jitter = lambda: 0.0
        self.client._sleep = self._sleep

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.clock += timedelta(seconds=seconds)

    @property
    def hits(self) -> int:
        return len(self.requests)


class AsyncHarness:
    """Harness for the asyncio client. Its sleeps move the clock and yield to other tasks."""

    def __init__(self, handler: Handler, **options: Any) -> None:
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.clock = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)

        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        options.setdefault("token", "test-token")
        self.client = AsyncClient(
            "mytool/1.2", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)), **options
        )
        self.client._now = lambda: self.clock
        self.client._jitter = lambda: 0.0
        self.client._sleep = self._sleep

    async def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        self.clock += timedelta(seconds=seconds)

    @property
    def hits(self) -> int:
        return len(self.requests)


@pytest.fixture
def harness() -> Callable[..., Harness]:
    return Harness
