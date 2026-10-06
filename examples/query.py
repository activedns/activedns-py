"""Run one search and print the first page of the answer.

    python examples/query.py example.com
    python examples/query.py --limit 10 '*.example.com'
    python examples/query.py 192.0.2.0/24

Uses the token built into the SDK; set ACTIVEDNS_TOKEN to use your own.
"""

import argparse
import os

from activedns import Client

parser = argparse.ArgumentParser(description="Search ActiveDNS")
parser.add_argument("query", help="domain, *.domain, address, network or AS number")
parser.add_argument("--limit", type=int, default=25, help="records to fetch")
args = parser.parse_args()

# the name tells the operators of the API which program is asking
with Client("example-query/1.0", token=os.environ.get("ACTIVEDNS_TOKEN")) as client:
    page = client.query(args.query, page_size=args.limit)

    for r in page.records:
        seen = f"first seen {r.first_seen:%Y-%m-%d}, last {r.observed:%Y-%m-%d}"
        print(f"{r.domain:40} {r.ip_address:39} {seen}")

    count = str(page.count)
    if page.count_estimated:
        count = "about " + count
    elif page.count_capped:
        count = "at least " + count
    more = f" (more from cursor {page.next_cursor})" if page.has_more else ""
    print(f"\n{page.query_type} search for {page.query}: {len(page.records)} of {count} records{more}")
    if page.timed_out:
        print("the server gave up on the search: the answer may be incomplete")

    # what the server said about the rate limit with this answer
    for limit in client.rate_limits:
        print(f'rate limit "{limit.name}": {limit.remaining} requests left of {limit.limit}')
