"""Entry point: `tapetide-mcp` / `uvx tapetide-mcp` / `python -m tapetide_mcp`."""

from __future__ import annotations

import json
import os
import signal
import sys

from . import __version__
from .auth import AuthError
from .remote import RemoteClient, jsonrpc_error
from .stdio import read_messages

TOKEN_URL = "https://tapetide.com/settings/tokens"


def _is_notification(msg: object) -> bool:
    # JSON-RPC 2.0: a notification has a method and no id, and gets no response.
    return isinstance(msg, dict) and "method" in msg and "id" not in msg


def _message_id(text: str) -> object:
    try:
        msg = json.loads(text)
    except ValueError:
        return None
    return msg.get("id") if isinstance(msg, dict) else None


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        print(__version__)
        return

    token = os.environ.get("TAPETIDE_TOKEN")
    if not token:
        sys.stderr.write(f"Error: TAPETIDE_TOKEN environment variable is required.\nGet one at {TOKEN_URL}\n")
        sys.exit(1)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    remote = RemoteClient(
        os.environ.get("TAPETIDE_MCP_URL", "https://mcp.tapetide.com").rstrip("/"),
        token,
        debug=os.environ.get("TAPETIDE_DEBUG") == "1",
    )

    try:
        remote.tokens.get()  # pre-authenticate so the first request is fast
    except AuthError as e:
        sys.stderr.write(f"Error: Failed to authenticate. Check your TAPETIDE_TOKEN.\n{e}\n")
        sys.exit(1)
    sys.stderr.write(f"Tapetide Stock Research MCP (Python) v{__version__} connected. Waiting for requests...\n")

    try:
        for text, write in read_messages(sys.stdin.buffer, sys.stdout.buffer):
            try:
                notification = _is_notification(json.loads(text))
            except ValueError:
                notification = False
            try:
                response = remote.forward(text)
                if not notification:
                    write(response)
            except Exception as e:  # noqa: BLE001 — any failure becomes a JSON-RPC error
                if not notification:
                    write(jsonrpc_error(_message_id(text), str(e) or "Internal error"))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
