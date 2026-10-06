# Examples

Each example is one file that runs as it is, once `activedns` is installed (`pip install activedns`).
The ones named `async_…` use `AsyncClient` and asyncio; the others use `Client`.

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

Every example explains itself at the top of its file and with `--help`.

They use the token built into the SDK; set `ACTIVEDNS_TOKEN` to use your own. `combined.py` needs one, and
shows the refusal without it. With your own token `query.py` and `async_query.py` also take AS numbers
(`AS64496`) and wider networks.
