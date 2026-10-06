"""List the names known under a domain, with the addresses each resolved to.

    python examples/subdomains.py example.com
    python examples/subdomains.py --pages 10 example.com

A query returns one page. The program decides how many more to fetch
(--pages), and says what it left behind.

Uses the token built into the SDK; set ACTIVEDNS_TOKEN to use your own.
"""

import argparse
import os
import sys

from activedns import Client, ForbiddenError, RateLimitError

parser = argparse.ArgumentParser(description="Names under a domain")
parser.add_argument("domain")
parser.add_argument("--pages", type=int, default=3, help="how many pages to fetch at most")
args = parser.parse_args()

addresses: dict[str, list[str]] = {}


def note(name: str, address: str) -> None:
    known = addresses.setdefault(name, [])
    if address not in known:
        known.append(address)


# the name tells the operators of the API which program is asking
with Client("example-subdomains/1.0", token=os.environ.get("ACTIVEDNS_TOKEN")) as client:
    fetched, pages = 0, 0
    try:
        page = client.query(f"*.{args.domain}")
        while page is not None:
            pages += 1
            fetched += len(page.records)
            for record in page.records:
                note(record.domain, record.ip_address)
                # names that reach the record's owner through a CNAME
                for alias in record.aliases:
                    note(alias.name, record.ip_address)

            # The page says whether there is more. Fetching it is a decision:
            # every page is a request against the rate limit.
            if page.has_more and pages == args.pages:
                print(
                    f"stopped at page {pages}: {fetched} of {page.count} records fetched; "
                    "--pages fetches more",
                    file=sys.stderr,
                )
                break
            page = client.next_page(page)
    except RateLimitError as err:
        # the client has already waited and retried; this wait was too long
        print(f"rate limit {err.exceeded!r} reached; try again at {err.retry_at}", file=sys.stderr)
    except ForbiddenError as err:
        # most often: the token may not page any deeper into the answer
        print(f"stopped by the token's policy: {err}", file=sys.stderr)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)

# what was found is printed, also when the listing stopped early
for name in sorted(addresses):
    print(f"{name:50} {' '.join(addresses[name])}")
print(f"{len(addresses)} names under {args.domain}", file=sys.stderr)
