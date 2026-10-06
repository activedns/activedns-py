"""Search for records that match a domain and a network and an AS number at once.

    ACTIVEDNS_TOKEN=… python examples/combined.py --domain '*.example.com' --asn 64496
    ACTIVEDNS_TOKEN=… python examples/combined.py --domain '*.example.com' --ip 192.0.2.0/24

Combined searches need a token of your own: the one built into the SDK may
not make them, and without ACTIVEDNS_TOKEN this example shows what that
refusal looks like.
"""

import argparse
import os
import sys

from activedns import Client, ForbiddenError, UnauthorizedError

parser = argparse.ArgumentParser(description="Combined ActiveDNS search")
parser.add_argument("--domain", help="a domain, or a wildcard such as *.example.com")
parser.add_argument("--ip", help="an address or a network")
parser.add_argument("--asn", type=int, help="an AS number, without the AS")
args = parser.parse_args()

with Client("example-combined/1.0", token=os.environ.get("ACTIVEDNS_TOKEN")) as client:
    try:
        page = client.query_combined(domain=args.domain, ip=args.ip, asn=args.asn)
    except ForbiddenError as err:
        # the token works, but not for this query; other queries still do
        sys.exit(f"this token may not make that search: {err}")
    except UnauthorizedError as err:
        sys.exit(f"the token is not accepted: {err}")

for r in page.records:
    print(f"{r.domain:40} {r.ip_address:39} AS{r.asn or 0:<8} {r.observed:%Y-%m-%d}")
print(f"\n{page.query}: {len(page.records)} of {page.count} records")
