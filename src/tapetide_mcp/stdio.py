"""Stdio framing: newline-delimited JSON (MCP spec) or Content-Length framed (LSP style).

The mode is auto-detected from the first bytes: a `{` means newline-delimited.
"""

from __future__ import annotations

import re
from typing import BinaryIO, Callable, Iterator

_CONTENT_LENGTH = re.compile(rb"Content-Length:\s*(\d+)", re.IGNORECASE)

Writer = Callable[[str], None]


def newline_writer(out: BinaryIO) -> Writer:
    def write(json_text: str) -> None:
        out.write(json_text.encode("utf-8") + b"\n")
        out.flush()

    return write


def framed_writer(out: BinaryIO) -> Writer:
    def write(json_text: str) -> None:
        body = json_text.encode("utf-8")
        out.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        out.flush()

    return write


def read_messages(stream: BinaryIO, out: BinaryIO) -> Iterator[tuple[str, Writer]]:
    """Yield (message, writer) pairs; the writer replies in the detected framing."""
    head = stream.readline()
    while head and not head.strip():
        head = stream.readline()
    if not head:
        return

    if head.lstrip().startswith(b"{"):
        write = newline_writer(out)
        line = head
        while line:
            text = line.decode("utf-8", "replace").strip()
            if text:
                yield text, write
            line = stream.readline()
        return

    write = framed_writer(out)
    header = head
    while header:
        headers = header
        # Collect header lines until the blank separator line.
        while header and header.strip():
            header = stream.readline()
            headers += header
        match = _CONTENT_LENGTH.search(headers)
        if match:
            body = stream.read(int(match.group(1)))
            if body:
                yield body.decode("utf-8", "replace"), write
        header = stream.readline()
        while header and not header.strip():
            header = stream.readline()
