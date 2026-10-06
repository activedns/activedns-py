"""End-to-end tests: the programs in examples/, run as a user would run them.

Each one is started with the installed package and the token built into the
SDK, against the live API.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def run(example: str, *args: str) -> subprocess.CompletedProcess[str]:
    # without a token of the developer's own: the examples then behave as they do for a new user
    env = {k: v for k, v in os.environ.items() if k != "ACTIVEDNS_TOKEN"}
    return subprocess.run(
        [sys.executable, str(EXAMPLES / example), *args], capture_output=True, text=True, timeout=120, env=env
    )


def test_every_example_is_tested() -> None:
    tested = {"query.py", "subdomains.py", "combined.py", "async_query.py", "async_domains.py"}
    assert {path.name for path in EXAMPLES.glob("*.py")} == tested


def test_query() -> None:
    done = run("query.py", "--limit", "2", "example.com")
    assert done.returncode == 0, done.stderr
    assert "domain search for example.com: 2 of" in done.stdout
    assert 'rate limit "address"' in done.stdout


def test_subdomains() -> None:
    done = run("subdomains.py", "--pages", "1", "example.com")
    assert done.returncode == 0, done.stderr
    assert "example.com" in done.stdout
    assert "names under example.com" in done.stderr


def test_combined_shows_the_refusal() -> None:
    done = run("combined.py", "--domain", "*.example.com", "--asn", "13335")
    assert done.returncode == 1
    assert "this token may not make that search: 403" in done.stderr


def test_async_query() -> None:
    done = run("async_query.py", "--limit", "2", "example.com")
    assert done.returncode == 0, done.stderr
    assert "domain search for example.com: 2 of" in done.stdout
    assert 'rate limit "address"' in done.stdout


def test_async_domains() -> None:
    done = run("async_domains.py", "example.com", "example.org")
    assert done.returncode == 0, done.stderr
    first, second = done.stdout.splitlines()
    assert first.startswith("example.com ") and second.startswith("example.org ")
    assert "names on the first page" in first
