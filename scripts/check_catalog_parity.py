"""Fail if the README's tool catalog disagrees with the live server's `tools/list`.

The bridge forwards JSON-RPC verbatim, so the remote can add, rename or retire
tools without any change here — which is exactly how the prose goes stale.
`tools/list` needs no authentication, so this runs in CI without secrets.

Usage: python scripts/check_catalog_parity.py   (MCP_URL overrides the server)
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

MCP_URL = os.environ.get("MCP_URL", "https://mcp.tapetide.com")
README = Path(__file__).resolve().parent.parent / "README.md"
ROW = re.compile(r"^\|\s*`([a-z_][a-z0-9_]*)`\s*\|")


def documented_tools() -> set[str]:
    md = README.read_text(encoding="utf-8")
    start, end = md.find("<!-- tools:start"), md.find("<!-- tools:end")
    if start == -1 or end == -1:
        raise RuntimeError("README is missing the tools:start / tools:end markers.")
    return {m.group(1) for line in md[start:end].splitlines() if (m := ROW.match(line))}


def live_tools() -> set[str]:
    req = urllib.request.Request(
        f"{MCP_URL}/mcp",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "tapetide-mcp-python-catalog-parity",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        text = res.read().decode()
        if "text/event-stream" in res.headers.get("Content-Type", ""):
            text = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")][-1]
    tools = json.loads(text).get("result", {}).get("tools") or []
    if not tools:
        raise RuntimeError("tools/list returned no tools — refusing to compare against an empty catalog.")
    return {t["name"] for t in tools}


def main() -> int:
    documented, live = documented_tools(), live_tools()
    missing, stale = sorted(live - documented), sorted(documented - live)
    print(f"Documented in README: {len(documented)}\nLive on {MCP_URL}: {len(live)}")
    if not missing and not stale:
        print("\nCatalog parity OK.")
        return 0
    for label, names in (("Live but NOT documented", missing), ("Documented but NOT live", stale)):
        if names:
            print(f"\n{label} ({len(names)}):", *(f"  {n}" for n in names), sep="\n", file=sys.stderr)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 — a message beats a stack trace in CI
        print(f"\nCatalog parity check could not run: {e}", file=sys.stderr)
        sys.exit(1)
