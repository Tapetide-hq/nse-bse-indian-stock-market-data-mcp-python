"""Bridge behaviour against a local fake of the remote MCP server."""

from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tapetide_mcp.remote import RemoteClient, extract_json_from_sse
from tapetide_mcp.stdio import read_messages


class FakeRemote(BaseHTTPRequestHandler):
    seen: list = []
    token_calls = 0
    reject_next = False

    def log_message(self, *_):
        pass

    def _send(self, status: int, body: str, headers: dict | None = None):
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body.encode())))
        self.end_headers()
        self.wfile.write(body.encode())

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"])).decode()
        if self.path == "/token":
            FakeRemote.token_calls += 1
            return self._send(200, json.dumps({"access_token": f"at{FakeRemote.token_calls}", "expires_in": 3600}))
        FakeRemote.seen.append(self.headers)
        if "Authorization" not in self.headers and json.loads(raw).get("method") != "tools/list":
            return self._send(401, '{"error":"authentication_required"}')
        if FakeRemote.reject_next:
            FakeRemote.reject_next = False
            return self._send(401, '{"error":"invalid_token"}')
        msg = json.loads(raw)
        if msg.get("method") == "initialize":
            body = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2025-06-18"}})
            return self._send(200, f"event: message\ndata: {body}\n\n",
                              {"Content-Type": "text/event-stream", "Mcp-Session-Id": "sess-1"})
        if msg.get("method") == "boom":
            return self._send(500, '{"error_description":"upstream failed"}')
        self._send(200, json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}),
                   {"Content-Type": "application/json"})


@pytest.fixture
def client():
    FakeRemote.seen, FakeRemote.token_calls, FakeRemote.reject_next = [], 0, False
    server = HTTPServer(("127.0.0.1", 0), FakeRemote)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield RemoteClient(f"http://127.0.0.1:{server.server_port}", "tpt_rt_test", timeout=5)
    server.shutdown()


def init_msg():
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"clientInfo": {"name": "Claude Desktop", "version": "1.2"}}})


def test_initialize_captures_identity_and_unwraps_sse(client):
    reply = json.loads(client.forward(init_msg()))
    assert reply["result"]["protocolVersion"] == "2025-06-18"
    assert FakeRemote.seen[0]["Authorization"] == "Bearer at1"
    assert FakeRemote.seen[0]["User-Agent"].endswith("(Claude-Desktop/1.2)")

    client.forward('{"jsonrpc":"2.0","id":2,"method":"tools/list"}')
    follow_up = FakeRemote.seen[1]
    assert follow_up["Mcp-Session-Id"] == "sess-1"
    assert follow_up["MCP-Protocol-Version"] == "2025-06-18"


def test_session_id_cannot_inject_headers(client):
    client._capture_session_id("abc\r\nX-Evil: 1")
    assert client.session_id == "abcX-Evil1"


def test_401_refreshes_token_and_retries_once(client):
    client.tokens.get()
    FakeRemote.reject_next = True
    reply = json.loads(client.forward('{"jsonrpc":"2.0","id":3,"method":"tools/list"}'))
    assert "result" in reply
    assert FakeRemote.token_calls == 2
    assert FakeRemote.seen[-1]["Authorization"] == "Bearer at2"


def test_http_error_becomes_jsonrpc_error(client):
    reply = json.loads(client.forward('{"jsonrpc":"2.0","id":7,"method":"boom"}'))
    assert reply == {"jsonrpc": "2.0", "id": 7, "error": {"code": -32603, "message": "upstream failed"}}


def test_sse_keeps_last_json_payload():
    assert extract_json_from_sse('data: ping\n\ndata: {"a":1}\n\ndata: {"b":2}\n') == '{"b":2}'


def test_reads_newline_delimited_messages():
    stdin, stdout = io.BytesIO(b'{"id":1}\n\n{"id":2}\n'), io.BytesIO()
    msgs = list(read_messages(stdin, stdout))
    assert [m for m, _ in msgs] == ['{"id":1}', '{"id":2}']
    msgs[0][1]('{"ok":true}')
    assert stdout.getvalue() == b'{"ok":true}\n'


def test_reads_content_length_framed_messages():
    a, b = b'{"id":1}', '{"id":"é"}'.encode()
    stdin = io.BytesIO(b"Content-Length: %d\r\n\r\n%s" % (len(a), a) + b"Content-Length: %d\r\n\r\n%s" % (len(b), b))
    stdout = io.BytesIO()
    msgs = list(read_messages(stdin, stdout))
    assert [m for m, _ in msgs] == ['{"id":1}', '{"id":"é"}']
    msgs[0][1]('{"ok":1}')
    assert stdout.getvalue() == b'Content-Length: 8\r\n\r\n{"ok":1}'


@pytest.fixture
def anonymous(client):
    return RemoteClient(client._url.removesuffix("/mcp"), None, timeout=5)


def test_discovery_mode_answers_initialize_locally(anonymous):
    reply = json.loads(anonymous.forward(init_msg()))
    assert reply["result"]["serverInfo"]["name"] == "tapetide"
    assert "TAPETIDE_TOKEN is not set" in reply["result"]["instructions"]
    assert FakeRemote.seen == []  # nothing sent upstream


def test_discovery_mode_forwards_public_methods_without_auth(anonymous):
    reply = json.loads(anonymous.forward('{"jsonrpc":"2.0","id":2,"method":"tools/list"}'))
    assert "result" in reply
    assert "Authorization" not in FakeRemote.seen[0]


def test_discovery_mode_rejects_tool_calls(anonymous):
    with pytest.raises(Exception, match="TAPETIDE_TOKEN is not set"):
        anonymous.forward('{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"x"}}')
