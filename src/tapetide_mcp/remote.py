"""Forward one JSON-RPC message to the remote MCP endpoint and return its reply.

The remote runs a stateless Streamable HTTP transport. The bridge forwards
messages verbatim and only carries identity across calls: the downstream
client's name (from `initialize`) in the User-Agent, the negotiated protocol
version, and the `Mcp-Session-Id` the remote mints at `initialize`.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from typing import Any

from . import __version__
from .auth import AuthError, TokenManager

TOKEN_HELP = "Get a free token at https://tapetide.com/settings/tokens and set it as TAPETIDE_TOKEN."

# Methods the remote serves without a token. Exact names, never a prefix, so
# nothing added later inherits anonymous access by accident.
PUBLIC_METHODS = frozenset({"tools/list", "ping", "resources/list", "resources/read"})
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


def is_public_method(method: object) -> bool:
    return isinstance(method, str) and (method in PUBLIC_METHODS or method.startswith("notifications/"))

_HEADER_TOKEN = re.compile(r"[^A-Za-z0-9._-]")
_SESSION_ID = re.compile(r"[^A-Za-z0-9._~+/=-]")


def _sanitize(value: str) -> str:
    """RFC 7230 token chars only — client-supplied names are untrusted header input."""
    return _HEADER_TOKEN.sub("-", value)[:64]


def _parse(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def extract_json_from_sse(sse: str) -> str:
    """Return the last `data:` payload that parses as JSON — the JSON-RPC response."""
    last = ""
    for line in sse.split("\n"):
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data and _parse(data) is not None:
            last = data
    return last or sse


def jsonrpc_error(msg_id: Any, message: str) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": message}})


class RemoteClient:
    def __init__(self, base_url: str, refresh_token: str | None, timeout: float = 30.0, debug: bool = False):
        self._url = f"{base_url}/mcp"
        self._timeout = timeout
        self._debug = debug
        self.client: str | None = None
        self.protocol_version: str | None = None
        self.session_id: str | None = None
        self.tokens = TokenManager(base_url, refresh_token, self.user_agent, timeout) if refresh_token else None
        # Why authentication is unavailable, or None when it works. Without a working
        # token the bridge runs in DISCOVERY MODE instead of exiting: registries,
        # inspectors and users who haven't pasted a token yet still get
        # `initialize` + `tools/list`, which the remote serves anonymously.
        self.auth_unavailable: str | None = None

    def try_access_token(self) -> str | None:
        if self.tokens is None:
            self.auth_unavailable = f"TAPETIDE_TOKEN is not set. {TOKEN_HELP}"
            return None
        try:
            token = self.tokens.get()
        except AuthError as e:
            self.auth_unavailable = f"{e}. Check your TAPETIDE_TOKEN. {TOKEN_HELP}"
            return None
        self.auth_unavailable = None
        return token

    def user_agent(self) -> str:
        own = f"tapetide-mcp-python/{__version__}"
        return f"{own} ({self.client})" if self.client else own

    def forward(self, body: str) -> str:
        msg = _parse(body)
        method = msg.get("method") if isinstance(msg, dict) else None

        # Identity must be set before the initialize call itself goes out, and a new
        # handshake drops the old session — the remote mints a fresh id for it.
        if method == "initialize":
            self._capture_client_info(msg)
            self.session_id = None

        token = self.try_access_token()
        if token is None:
            if method == "initialize":
                return self._local_initialize(msg)
            if not is_public_method(method):
                raise AuthError(self.auth_unavailable or TOKEN_HELP)

        status, headers, text = self._post(body, token)
        if status == 401 and self.tokens is not None and token is not None:
            self.tokens.invalidate()
            status, headers, text = self._post(body, self.tokens.get())

        self._warn_on_rate_limit(headers)
        if method == "initialize":
            self._capture_session_id(headers.get("Mcp-Session-Id"))

        if "text/event-stream" in (headers.get("Content-Type") or ""):
            text = extract_json_from_sse(text)

        if method == "initialize":
            self._capture_protocol_version(text)
        if self._debug:
            sys.stderr.write(f"[debug] {method or '?'} → {status}\n")

        if status >= 400:
            msg_id = msg.get("id") if isinstance(msg, dict) else None
            return jsonrpc_error(msg_id, self._error_message(status, text))
        return text

    def _local_initialize(self, msg: dict) -> str:
        """Answer `initialize` in discovery mode — the remote requires a token for it."""
        requested = (msg.get("params") or {}).get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else "2025-06-18"
        self.protocol_version = version
        return json.dumps({
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": True}, "resources": {"listChanged": True}},
                "serverInfo": {"name": "tapetide", "version": __version__},
                "instructions": "Tapetide is running without authentication: tools can be listed "
                f"but not called. {self.auth_unavailable}",
            },
        })

    def _post(self, body: str, token: str | None) -> tuple[int, Any, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": self.user_agent(),
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self._url, data=body.encode(), method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as res:
                return res.status, res.headers, res.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode("utf-8", "replace")

    def _capture_client_info(self, msg: dict) -> None:
        info = (msg.get("params") or {}).get("clientInfo") or {}
        name = info.get("name")
        if not isinstance(name, str) or not name:
            return
        ver = info.get("version")
        self.client = f"{_sanitize(name)}/{_sanitize(ver)}" if isinstance(ver, str) and ver else _sanitize(name)

    def _capture_session_id(self, raw: str | None) -> None:
        # The header goes straight back on the wire; a hostile TAPETIDE_MCP_URL
        # must not be able to inject CR/LF through it.
        if raw:
            self.session_id = _SESSION_ID.sub("", raw) or None

    def _capture_protocol_version(self, text: str) -> None:
        # Read from the remote's RESPONSE: echoing the version it chose can never fail.
        msg = _parse(text)
        if isinstance(msg, dict):
            ver = (msg.get("result") or {}).get("protocolVersion")
            if isinstance(ver, str) and ver:
                self.protocol_version = ver

    @staticmethod
    def _warn_on_rate_limit(headers: Any) -> None:
        # `Retry-After` is only present when a call was denied by the quota.
        retry_after = headers.get("Retry-After")
        if retry_after is None:
            return
        when = f"{retry_after}s" if retry_after.strip().isdigit() else "shortly"
        sys.stderr.write(
            f"Warning: Tapetide rate limit reached — request denied. Retry in {when}. "
            "Check usage at https://tapetide.com/settings/tokens\n"
        )

    @staticmethod
    def _error_message(status: int, text: str) -> str:
        err = _parse(text)
        if isinstance(err, dict):
            for key in ("error_description", "message", "error"):
                if isinstance(err.get(key), str) and err[key]:
                    return err[key]
        return text[:200] if text else f"Remote error ({status})"
