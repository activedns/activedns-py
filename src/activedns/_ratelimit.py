"""The rate-limit headers of the API.

The API reports its limits in the form of the IETF httpapi draft
(draft-ietf-httpapi-ratelimit-headers)::

    RateLimit-Policy: "address";q=60;w=120, "network";q=300;w=120
    RateLimit: "address";r=41;t=2

``q`` is how many requests the limit allows at once, ``r`` how many are left
right now, and ``t`` the seconds until one more is available.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from ._models import RateLimit


def parse_rate_limits(headers: Mapping[str, str], now: datetime) -> tuple[RateLimit, ...]:
    """Read the RateLimit and RateLimit-Policy headers of a response."""
    sizes = {}
    for member in _split_list(headers.get("RateLimit-Policy", "")):
        name, params = _item(member)
        if "q" in params:
            sizes[name] = params["q"]

    limits = []
    for member in _split_list(headers.get("RateLimit", "")):
        name, params = _item(member)
        if "r" not in params:
            continue
        reset = now + timedelta(seconds=params["t"]) if params.get("t", -1) >= 0 else None
        limits.append(RateLimit(name=name, remaining=params["r"], limit=sizes.get(name), reset=reset))
    return tuple(limits)


def exhausted_until(limits: Sequence[RateLimit], now: datetime) -> tuple[datetime, str] | None:
    """The latest reset among the limits with nothing left, and its name.

    Before that moment another request would be refused. ``None`` when every
    limit has requests left.
    """
    found: tuple[datetime, str] | None = None
    for limit in limits:
        if limit.remaining <= 0 and limit.reset is not None and limit.reset > now:
            if found is None or limit.reset > found[0]:
                found = (limit.reset, limit.name)
    return found


def _split_list(value: str) -> list[str]:
    """Split a header value on the commas outside quoted strings."""
    parts, start, quoted = [], 0, False
    for i, char in enumerate(value):
        if char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            parts.append(value[start:i])
            start = i + 1
    parts.append(value[start:])
    return [p.strip() for p in parts if p.strip()]


def _item(member: str) -> tuple[str, dict[str, int]]:
    """Split ``"name";a=1;b=2`` into the name and its integer parameters."""
    name, *rest = member.split(";")
    params = {}
    for part in rest:
        key, _, value = part.partition("=")
        try:
            params[key.strip()] = int(value.strip())
        except ValueError:
            continue
    return name.strip().strip('"'), params
