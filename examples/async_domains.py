"""Search several domains at the same time, one task each, through one client.

    python examples/async_domains.py example.com example.org example.net
    python examples/async_domains.py --concurrent 4 example.com example.org example.net

For each domain it prints how many records there are under it, and how many
names the first page of the answer holds.

The tasks share one AsyncClient, and with it one pacing: the client sends at
most --concurrent requests at once, and when the API tells one task to slow
down, all of them wait.

Uses the token built into the SDK; set ACTIVEDNS_TOKEN to use your own.
"""

import argparse
import asyncio
import os

from activedns import ActiveDNSError, AsyncClient

parser = argparse.ArgumentParser(description="Names under several domains, searched concurrently")
parser.add_argument("domains", nargs="+", metavar="domain")
parser.add_argument("--concurrent", type=int, default=2, help="requests in flight at once")
args = parser.parse_args()


async def names_under(client: AsyncClient, domain: str) -> str:
    """Search one domain and return the line to print for it."""
    try:
        page = await client.query(f"*.{domain}")
    except ActiveDNSError as err:
        # one search that fails does not stop the others
        return f"{domain:30} {err}"
    names = {record.domain for record in page.records}
    more = ", and there are more pages" if page.has_more else ""
    return f"{domain:30} {page.count} records; {len(names)} names on the first page{more}"


async def main() -> None:
    # One client for every task. A client per task would also work, but then
    # nothing would keep the tasks from all sending at once.
    async with AsyncClient(
        "example-async-domains/1.0",
        token=os.environ.get("ACTIVEDNS_TOKEN"),
        max_concurrent=args.concurrent,
    ) as client:
        # gather returns the lines in the order of the domains
        for line in await asyncio.gather(*(names_under(client, domain) for domain in args.domains)):
            print(line)


asyncio.run(main())
