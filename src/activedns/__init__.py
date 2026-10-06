"""Client for the ActiveDNS API (https://activedns.net).

DNS search by domain, address, network and AS number::

    from activedns import Client

    with Client("mytool/1.0") as client:
        page = client.query("*.example.com")
        for record in page.records:
            print(record.domain, record.ip_address)

A query returns one page. ``page.count`` is how many records match in all and
``page.has_more`` whether there are more after this page;
``client.next_page(page)`` fetches them. The client never fetches further
pages by itself.

A query is one of::

    example.com      records of a domain
    *.example.com    records of a domain and of every name under it
    192.0.2.1        names that resolve to an address
    192.0.2.0/24     names that resolve into a network
    AS64496          names that resolve into an AS

Requests are authenticated with a token. The SDK has one built in, shared by
all its users, so no account is needed. It allows domain, wildcard, address
and network queries, small pages and the first records of a result. A token
issued to you (``Client(app, token=...)``) adds AS-number and combined
queries, wider networks, larger pages, deeper paging and a higher rate limit.
Request one at https://activedns.net/contact/.
"""

from ._client import DEFAULT_BASE_URL, Client
from ._errors import (
    ActiveDNSError,
    APIError,
    ForbiddenError,
    RateLimitError,
    TransportError,
    UnauthorizedError,
)
from ._models import Alias, Page, RateLimit, Record
from ._version import __version__

__all__ = [
    "DEFAULT_BASE_URL",
    "APIError",
    "ActiveDNSError",
    "Alias",
    "Client",
    "ForbiddenError",
    "Page",
    "RateLimit",
    "RateLimitError",
    "Record",
    "TransportError",
    "UnauthorizedError",
    "__version__",
]
