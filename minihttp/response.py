"""Build and serialise HTTP responses."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from email.utils import formatdate
from http import HTTPStatus
from typing import Any


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def text(cls, text: str, status: int = 200) -> Response:
        return cls(status, text.encode("utf-8"), {"Content-Type": "text/plain; charset=utf-8"})

    @classmethod
    def json(cls, data: Any, status: int = 200) -> Response:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        return cls(status, body, {"Content-Type": "application/json"})

    @classmethod
    def error(cls, status: int, message: str) -> Response:
        return cls.json({"error": HTTPStatus(status).phrase, "detail": message}, status)


def reason(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Unknown"


def serialize(response: Response, *, keep_alive: bool, head_only: bool = False) -> bytes:
    """Status line + headers + body. Content-Length is always set from the real body
    (even for HEAD, which reports the size without sending the bytes)."""
    no_body = head_only or response.status == 304 or 100 <= response.status < 200 or response.status == 204
    headers = {
        "Date": formatdate(usegmt=True),
        "Server": "minihttp",
        **response.headers,
        "Connection": "keep-alive" if keep_alive else "close",
    }
    if response.status not in (204, 304) and not 100 <= response.status < 200:
        headers["Content-Length"] = str(len(response.body))
    for name, value in headers.items():
        if "\r" in value or "\n" in value:  # never let data inject extra headers
            raise ValueError(f"Header {name!r} contains a line break")
    head = f"HTTP/1.1 {response.status} {reason(response.status)}\r\n"
    head += "".join(f"{name}: {value}\r\n" for name, value in headers.items())
    return (head + "\r\n").encode("latin-1") + (b"" if no_body else response.body)
