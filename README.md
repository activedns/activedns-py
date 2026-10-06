# activedns

Python client for the [ActiveDNS](https://activedns.net) API: DNS search by domain, address, network
and AS number. Python 3.10 or later.

```
pip install activedns
```

```python
from activedns import Client

# name your program: it is sent in the User-Agent of every request
with Client("mytool/1.0") as client:
    page = client.query("*.example.com")
    for record in page.records:
        print(record.domain, record.ip_address, record.observed)
```

A query is a domain (`example.com`), a wildcard (`*.example.com`), an address, a network (`192.0.2.0/24`) or
an AS number (`AS13335`). `query_combined` matches several at once.

`query` returns one page of the result and tells you whether there is more; see [Paging](#paging).

It works without an account or a key. AS-number and combined searches, wider networks, complete answers and
higher rates come with [a token of your own](#your-own-token).

## Paging

A query returns one page: up to 100 records, or as many as `page_size` asks for. The page tells you whether
that was everything:

| attribute | meaning |
|---|---|
| `page.count` | how many records match in all (`count_estimated`: an estimate; `count_capped`: at least that many) |
| `page.has_more` | there are records after this page |
| `len(page.records)` | how many you got |

The client never fetches further pages by itself. Each one is a request against your rate limit, so how much of
a large result to retrieve is your decision, made with `next_page`:

```python
page = client.query("*.example.com")
fetched = 0
while page is not None:
    use(page.records)
    fetched += len(page.records)
    if fetched >= 500:  # enough for this job
        break
    page = client.next_page(page)
```

`next_page` repeats the query from where the page ended, and returns `None` after the last page. Past the depth
your token may page to, it raises `ForbiddenError`.

## Async

`AsyncClient` is the same client for asyncio, on `httpx.AsyncClient`. It takes the same arguments, has the same
methods, to be awaited, and raises the same errors:

```python
import asyncio
from activedns import AsyncClient


async def main():
    async with AsyncClient("mytool/1.0") as client:
        com, net = await asyncio.gather(
            client.query("*.example.com"),
            client.query("*.example.net"),
        )
        more = await client.next_page(com)


asyncio.run(main())
```

Tasks that share a client share its pacing: it sends one request at a time unless `max_concurrent` says
otherwise, and when the API tells one task to slow down, all of them wait. See [A fair client](#a-fair-client).
Without `async with`, close the client with `await client.aclose()`.

## Your own token

The SDK works the moment you install it, because it carries a token of its own. That token is shared by
everyone who uses the SDK, so it is deliberately modest. **A token issued to you opens up considerably more of
the database**, and using one is a single argument:

```python
client = Client("mytool/1.0", token=os.environ["ACTIVEDNS_TOKEN"])
```

Nothing else in your code changes: the same `query`, `query_combined` and `next_page`, with more allowed.

### What a token of your own gives you

| | built-in token | your own token |
|---|---|---|
| **Search by AS number** | no | yes: everything observed in a network operator's address space |
| **Combined searches** | no | yes: domain *and* network *and* AS number in one question |
| **Widest network** | /24 (256 addresses), IPv6 /48 | /16 (65,536 addresses) and wider |
| **Records per request** | 100 | 1,000 |
| **How far into an answer** | the first 200 records | 10,000 records and beyond |
| **Request rate** | 30 a minute, 60 at once | 120 a minute, 240 at once, and higher |
| **What the rate counts** | your network address, and the network around it | the token: the same allowance from a laptop, a CI job or a fleet |
| **Place in the queue** | after everyone else | ahead of all shared-token traffic |

The right-hand column is where tokens start, not where they end: every token has its own limits on the server,
set for what its holder is building. The numbers are those of October 2026.

### What that makes possible

**Map an organisation by its AS number.** Every name seen resolving into a network operator's address space,
without knowing a single domain beforehand:

```python
page = client.query("AS64496")
```

**Ask precise questions.** What does this company host at that provider? Which names under a domain sit in
one particular network? A combined search answers in one request what would otherwise be thousands of records
to download and filter yourself:

```python
page = client.query_combined(domain="*.example.com", asn=64496)
page = client.query_combined(domain="*.example.com", ip="192.0.2.0/24")
```

**Sweep whole networks.** A /16 in one search instead of 256 separate /24s:

```python
page = client.query("198.51.0.0/16")
```

**Get the whole answer.** The built-in token shows the first 200 records of a search, which is enough to look
around. Large zones, hosting ranges and CDNs have tens of thousands; with your own token `next_page` keeps
going, 1,000 records at a time if you ask for pages that size:

```python
# every name under a large zone, not only the first 200 records
names = set()
page = client.query("*.example.com", page_size=1000)
while page is not None:
    names.update(record.domain for record in page.records)
    page = client.next_page(page)
print(len(names), "names")
```

With the built-in token the same loop ends after 200 records with a `ForbiddenError`:
`403: cursor beyond your token's limit of 100`.

**Run it where your work runs.** The built-in token's allowance belongs to a network address, so an office, a
cloud region or a CI provider's address range shares it with whoever else is there. Your own token's allowance
is yours wherever it is used: pipelines, scheduled jobs and several machines at once. Keep the token out of the
code and hand it to the job as a secret:

```yaml
# GitHub Actions
- run: python -m asset_monitor
  env:
    ACTIVEDNS_TOKEN: ${{ secrets.ACTIVEDNS_TOKEN }}
```

**Stay fast when the service is busy.** Searches are served by priority. The website comes first, issued
tokens next, and the tokens shared by tools and SDKs last.

### When you have outgrown the built-in token

The API tells you. A search the built-in token may not make raises a `ForbiddenError` that says what was
missing:

```
403: query type asn is not allowed for your token
403: combined queries (domain AND ip AND asn) are not allowed for your token
403: IPv4 network too wide for your token: prefix must be /24 or longer
403: cursor beyond your token's limit of 100; narrow the query instead
```

A `RateLimitError` whose `exceeded` is `address` or `network` on work you run regularly means the same thing.

### Getting one

Write to us through [activedns.net/contact](https://activedns.net/contact/) and say what you are building and
roughly how much you expect to query. Tokens are issued by hand, with limits to fit: a research project, an
integration in your product and a security team's daily monitoring need different things.

### About the built-in token

It identifies the SDK to the API, whatever User-Agent a request has, and it is not a secret. It allows domain,
wildcard (`*.example.com`), address and network searches within the limits in the table above. Its rate limit
counts per network address (an IPv4 address, an IPv6 /64), with a second, larger one for the network around it
(IPv4 /24, IPv6 /48), so that the users of the SDK do not spend each other's allowance.

## Say who you are

`Client` takes the name and version of your program and refuses to work without one. Requests then carry

```
User-Agent: mytool/1.0 activedns-py/0.1.0 (python 3.12.3; linux)
```

Everyone using the SDK's token looks the same to the API otherwise. With a name, a tool that misbehaves can be
told apart from the rest, and its author asked about it instead of everyone being limited.

## Examples

Five small programs in `examples/`, each one file that runs as it is. The ones named `async_…` use
`AsyncClient`; the others use `Client`:

| example | client | what it shows |
|---|---|---|
| `query.py` | `Client` | one search: the records of a page, the count, the rate limit |
| `subdomains.py` | `Client` | paging with `next_page`, and stopping at a rate limit or the token's depth |
| `combined.py` | `Client` | `query_combined`, and the refusal when the token does not allow a search |
| `async_query.py` | `AsyncClient` | `query.py` with asyncio: the same search, awaited |
| `async_domains.py` | `AsyncClient` | several searches at once, as tasks that share one client |

```
python examples/query.py example.com
python examples/query.py --limit 10 192.0.2.0/24
python examples/subdomains.py --pages 5 example.com
python examples/combined.py --domain '*.example.com' --asn 64496
python examples/async_query.py example.com
python examples/async_domains.py example.com example.org example.net
```

They use the SDK's own token; set `ACTIVEDNS_TOKEN` to use yours. `combined.py` needs one, and shows the
refusal without it; `query.py` and `async_query.py` with your own token also take AS numbers (`AS64496`) and
wider networks.

## Errors

Every error is an `ActiveDNSError`:

| error | meaning |
|---|---|
| `RateLimitError` | a rate limit is reached and the client did not wait it out; `exceeded` names it, `retry_at` says when to come back |
| `ForbiddenError` (403) | the token may not make that query; other queries go on working |
| `UnauthorizedError` (401) | the token is not accepted; the client sends nothing more |
| `APIError` | any other error response, with `status_code` and `message` |
| `TransportError` | no answer: a connection failure or a timeout |

```python
from activedns import RateLimitError

try:
    page = client.query("*.example.com")
except RateLimitError as err:
    # err.exceeded: "address", "network" or "token"
    # err.retry_at: when to try again
    ...
```

## A fair client

The API is shared, so the client holds itself back without being asked:

| situation | what the client does |
|---|---|
| several threads or tasks query at once | one request at a time (`max_concurrent` to change) |
| 429, 502, 503, 504, connection failure | up to 4 retries (`max_retries`), waiting 1 s, 2 s, 4 s, 8 s … (at most 30 s) with jitter, and at least `Retry-After` |
| told to slow down | every thread or task using the client waits, not only the one that was told |
| asked to wait longer than 30 s (`max_wait`) | no sleeping: the call raises `RateLimitError` with `retry_at`, and later calls fail at once until then |
| a response says no requests are left | the next request waits for the limit to reset instead of being sent and refused |
| the token is refused (401) | nothing more is sent |
| bad query, forbidden query, server error, request timed out | raised as it is, never retried |
| the server timed out on a search (`Page.timed_out`) | the page is returned as it is, not retried |
| a result has more pages | nothing: further pages are fetched only when you call `next_page` |

Share one `Client` (or `AsyncClient`) across a program: these limits are kept per client. `client.rate_limits` holds the limits
the server reported with its latest response.

## Options

| argument | default |
|---|---|
| `token` | the SDK's own token |
| `max_concurrent` | 1 |
| `max_retries` | 4 |
| `max_wait` | 30 seconds |
| `timeout` | 60 seconds |
| `http_client` | a new `httpx.Client` (`httpx.AsyncClient` for `AsyncClient`) |
| `base_url` | `https://activedns.net` |

Records, pages and rate limits are immutable [attrs](https://www.attrs.org) classes.

## Development

```
pip install -e '.[dev]'
pytest                  unit tests, against a fake server
ruff check .
ruff format --check .
mypy
pytest e2e              about thirty requests to the live API, with the SDK's own token:
                        both clients, and every example
```

The same checks run in GitHub Actions on every push and pull request (`.github/workflows/ci.yml`): lint and
types, the unit tests on Python 3.10 to 3.14, and then the end-to-end tests against the built wheel.
