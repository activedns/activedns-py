from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from attrs import field, frozen


@frozen
class Alias:
    """A CNAME name that resolves to a record's domain.

    Attributes:
        name: The alias.
        chain: The names from ``name`` to the record's domain, in order.
    """

    name: str
    chain: tuple[str, ...] = ()


@frozen
class Record:
    """One observation: a name that resolved to an address.

    Attributes:
        id: Identifier of the record.
        ip_address: The address.
        domain: The name that holds the address. For a name that is a CNAME,
            the name at the end of the chain; see ``aliases``.
        observed: The most recent observation.
        first_seen: The first observation.
        ip_version: 4 or 6.
        asn: AS number of the address, or ``None`` when unknown.
        country: Country code of the address, or ``None`` when unknown.
        aliases: The CNAME names that matched a name query and lead to
            ``domain``. Empty when ``domain`` itself matched.
        known_as: Some of the CNAME names that point at ``domain``. Set on
            address, network and AS-number queries.
        known_as_total: How many such names there are, capped at 1000.
    """

    id: int
    ip_address: str
    domain: str
    observed: datetime
    first_seen: datetime
    ip_version: int
    asn: int | None = None
    country: str | None = None
    aliases: tuple[Alias, ...] = ()
    known_as: tuple[Alias, ...] = ()
    known_as_total: int = 0


@frozen
class Page:
    """One page of results.

    ``count`` and ``has_more`` say whether there is more than this page;
    :meth:`Client.next_page` fetches the next one.

    Attributes:
        records: The records of this page.
        count: Number of records that match the query in all, not the
            number in this page. An estimate when ``count_estimated`` is
            set, a lower bound when ``count_capped`` is set.
        count_estimated: See ``count``.
        count_capped: See ``count``.
        has_more: Whether there are records after this page.
        next_cursor: Offset of the first record after this page. Valid when
            ``has_more`` is true.
        timed_out: True when the server gave up on the search; ``records``
            may be incomplete. The client does not retry such a query.
        query: The query as the server normalised it.
        query_type: How the server classified it: ``"domain"``,
            ``"wildcard_domain"``, ``"ip"``, ``"cidr"``, ``"asn"`` or
            ``"combined"``.
    """

    records: tuple[Record, ...]
    count: int
    next_cursor: int
    has_more: bool
    query: str
    query_type: str
    count_estimated: bool = False
    count_capped: bool = False
    timed_out: bool = False
    # the request that produced the page, for Client.next_page
    _request: dict[str, Any] | None = field(default=None, repr=False, eq=False)


@frozen
class RateLimit:
    """A rate limit as the server reported it in the headers of a response.

    Attributes:
        name: The limit: ``"address"``, ``"network"`` or ``"token"``.
        limit: How many requests the limit allows at once, or ``None`` if
            not reported.
        remaining: How many requests can be made now.
        reset: When more requests become available, or ``None`` if not
            reported.
    """

    name: str
    remaining: int
    limit: int | None = None
    reset: datetime | None = None


_TIME = re.compile(r"^(?P<main>[^.Zz+]+?)(?:\.(?P<frac>\d+))?(?P<zone>[Zz]|[+-]\d\d:\d\d)$")


def parse_time(value: str) -> datetime:
    """Parse an RFC 3339 timestamp, with any number of fractional digits."""
    m = _TIME.match(value)
    if m is None:
        return datetime.fromisoformat(value)
    frac = (m["frac"] or "")[:6].ljust(6, "0")
    zone = "+00:00" if m["zone"] in "Zz" else m["zone"]
    return datetime.fromisoformat(f"{m['main']}.{frac}{zone}")


def _aliases(items: Any) -> tuple[Alias, ...]:
    return tuple(Alias(name=a["name"], chain=tuple(a.get("chain") or ())) for a in items or ())


def record_from_dict(data: dict[str, Any]) -> Record:
    return Record(
        id=data["id"],
        ip_address=data["ip_address"],
        domain=data["domain"],
        observed=parse_time(data["observed"]),
        first_seen=parse_time(data["first_seen"]),
        ip_version=data["ip_version"],
        asn=data.get("asn"),
        country=data.get("country"),
        aliases=_aliases(data.get("aliases")),
        known_as=_aliases(data.get("known_as")),
        known_as_total=data.get("known_as_total", 0),
    )


def page_from_dict(data: dict[str, Any], request: dict[str, Any] | None = None) -> Page:
    return Page(
        request=request,
        records=tuple(record_from_dict(r) for r in data.get("records") or ()),
        count=data.get("count", 0),
        next_cursor=data.get("next_cursor", 0),
        has_more=data.get("has_more", False),
        query=data.get("query", ""),
        query_type=data.get("query_type", ""),
        count_estimated=data.get("count_estimated", False),
        count_capped=data.get("count_capped", False),
        timed_out=data.get("timed_out", False),
    )
