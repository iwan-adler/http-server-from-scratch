"""Parse HTTP/1.x requests from a byte stream (RFC 9112), defensively.

Everything here treats the client as untrusted: every read is bounded, and
anything ambiguous is rejected with a precise status code rather than guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import BinaryIO
from urllib.parse import parse_qs, unquote, urlsplit

MAX_REQUEST_LINE = 8 * 1024
MAX_HEADER_LINE = 8 * 1024
MAX_HEADERS = 100
MAX_BODY = 1024 * 1024

# RFC 9110 "token" characters, used for methods and header names.
TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
SUPPORTED_VERSIONS = ("HTTP/1.0", "HTTP/1.1")


class HTTPError(Exception):
    """A request that can't be served; carries the status to send back."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Headers:
    """Case-insensitive header map that keeps repeated headers."""

    def __init__(self) -> None:
        self._items: list[tuple[str, str]] = []

    def add(self, name: str, value: str) -> None:
        self._items.append((name, value))

    def get(self, name: str, default: str | None = None) -> str | None:
        values = self.get_all(name)
        return values[0] if values else default

    def get_all(self, name: str) -> list[str]:
        lowered = name.lower()
        return [v for n, v in self._items if n.lower() == lowered]

    def __contains__(self, name: str) -> bool:
        return bool(self.get_all(name))

    def __len__(self) -> int:
        return len(self._items)


@dataclass
class Request:
    method: str
    target: str
    version: str
    headers: Headers
    body: bytes = b""
    path: str = "/"
    query: dict[str, list[str]] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)  # filled in by the router

    @property
    def keep_alive(self) -> bool:
        """HTTP/1.1 keeps the connection open unless told otherwise; HTTP/1.0 is the reverse."""
        tokens = {t.strip().lower() for v in self.headers.get_all("Connection") for t in v.split(",")}
        if self.version == "HTTP/1.1":
            return "close" not in tokens
        return "keep-alive" in tokens


def _read_line(stream: BinaryIO, limit: int, status: int, what: str) -> bytes:
    line = stream.readline(limit + 1)
    if len(line) > limit:
        raise HTTPError(status, f"{what} too long (limit {limit} bytes)")
    return line


def read_request(stream: BinaryIO) -> Request | None:
    """Read one request. Returns None if the client closed the connection cleanly."""
    line = _read_line(stream, MAX_REQUEST_LINE, 414, "Request line")
    if line in (b"\r\n", b"\n"):  # RFC 9112 allows ignoring one empty line before a request
        line = _read_line(stream, MAX_REQUEST_LINE, 414, "Request line")
    if not line:
        return None
    if not line.endswith(b"\n"):
        raise HTTPError(400, "Incomplete request line")

    parts = line.decode("latin-1").rstrip("\r\n").split(" ")
    if len(parts) != 3:
        raise HTTPError(400, "Request line must be 'METHOD target HTTP/x.y'")
    method, target, version = parts
    if not TOKEN.match(method):
        raise HTTPError(400, "Invalid method")
    if not version.startswith("HTTP/"):
        raise HTTPError(400, "Invalid HTTP version")
    if version not in SUPPORTED_VERSIONS:
        raise HTTPError(505, f"{version} is not supported")
    if not (target.startswith("/") or (method == "OPTIONS" and target == "*")):
        raise HTTPError(400, "Only origin-form targets (starting with '/') are supported")

    headers = _read_headers(stream)
    if version == "HTTP/1.1" and "Host" not in headers:
        raise HTTPError(400, "HTTP/1.1 requests must include a Host header")
    if "Transfer-Encoding" in headers:
        raise HTTPError(501, "Transfer-Encoding (e.g. chunked) is not supported; send Content-Length")

    body = _read_body(stream, headers)
    split = urlsplit(target)
    path = unquote(split.path)
    if "\x00" in path:
        raise HTTPError(400, "Invalid path")
    return Request(method, target, version, headers, body, path=path,
                   query=parse_qs(split.query, keep_blank_values=True))


def _read_headers(stream: BinaryIO) -> Headers:
    headers = Headers()
    while True:
        line = _read_line(stream, MAX_HEADER_LINE, 431, "Header line")
        if not line:
            raise HTTPError(400, "Connection closed in the middle of the headers")
        if line in (b"\r\n", b"\n"):
            return headers
        if line[:1] in (b" ", b"\t"):
            raise HTTPError(400, "Folded header lines are not allowed")
        if len(headers) >= MAX_HEADERS:
            raise HTTPError(431, f"Too many headers (limit {MAX_HEADERS})")
        name, sep, value = line.decode("latin-1").rstrip("\r\n").partition(":")
        # No whitespace is allowed between the name and the colon (request smuggling risk).
        if not sep or not TOKEN.match(name):
            raise HTTPError(400, f"Malformed header line: {name[:40]!r}")
        headers.add(name, value.strip(" \t"))


def _read_body(stream: BinaryIO, headers: Headers) -> bytes:
    lengths = headers.get_all("Content-Length")
    if not lengths:
        return b""
    # Conflicting lengths are a classic request-smuggling trick: refuse them.
    values = {v.strip() for raw in lengths for v in raw.split(",")}
    if len(values) != 1 or not next(iter(values)).isdigit():
        raise HTTPError(400, "Invalid or conflicting Content-Length")
    length = int(next(iter(values)))
    if length > MAX_BODY:
        raise HTTPError(413, f"Body too large (limit {MAX_BODY} bytes)")
    body = stream.read(length)
    if len(body) != length:
        raise HTTPError(400, "Connection closed before the full body arrived")
    return body
