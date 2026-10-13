import io

import pytest

from minihttp.request import MAX_BODY, MAX_HEADERS, HTTPError, read_request


def parse(raw: bytes):
    return read_request(io.BytesIO(raw))


def status_of(raw: bytes) -> int:
    with pytest.raises(HTTPError) as excinfo:
        parse(raw)
    return excinfo.value.status


def test_parses_a_get_request():
    req = parse(b"GET /api/hello/x%20y?greeting=Hi&tag=a&tag=b HTTP/1.1\r\nHost: example\r\nX-Thing:  spaced  \r\n\r\n")
    assert (req.method, req.version, req.path) == ("GET", "HTTP/1.1", "/api/hello/x y")
    assert req.query == {"greeting": ["Hi"], "tag": ["a", "b"]}
    assert req.headers.get("x-thing") == "spaced"  # case-insensitive, trimmed
    assert req.body == b"" and req.keep_alive


def test_reads_exactly_content_length_bytes():
    stream = io.BytesIO(b"POST /e HTTP/1.1\r\nHost: h\r\nContent-Length: 5\r\n\r\nhelloGET /next HTTP/1.1\r\n")
    assert read_request(stream).body == b"hello"
    assert stream.read() == b"GET /next HTTP/1.1\r\n"  # the next pipelined request is untouched


def test_bare_lf_line_endings_and_a_leading_blank_line_are_tolerated():
    assert parse(b"\r\nGET / HTTP/1.1\nHost: h\n\n").path == "/"


def test_clean_close_returns_none():
    assert parse(b"") is None


@pytest.mark.parametrize(("version", "headers", "expected"), [
    ("HTTP/1.1", "", True),
    ("HTTP/1.1", "Connection: close\r\n", False),
    ("HTTP/1.1", "Connection: Upgrade, CLOSE\r\n", False),
    ("HTTP/1.0", "", False),
    ("HTTP/1.0", "Connection: keep-alive\r\n", True),
])
def test_keep_alive_rules(version, headers, expected):
    host = "Host: h\r\n" if version == "HTTP/1.1" else ""
    assert parse(f"GET / {version}\r\n{host}{headers}\r\n".encode()).keep_alive is expected


@pytest.mark.parametrize(("raw", "status"), [
    (b"GET /\r\n\r\n", 400),                                     # missing version
    (b"GET  / HTTP/1.1\r\nHost: h\r\n\r\n", 400),                # double space
    (b"G(T / HTTP/1.1\r\nHost: h\r\n\r\n", 400),                 # invalid method token
    (b"GET http://evil/ HTTP/1.1\r\nHost: h\r\n\r\n", 400),      # absolute-form not supported
    (b"GET / FTP/1.0\r\n\r\n", 400),
    (b"GET / HTTP/2.0\r\nHost: h\r\n\r\n", 505),
    (b"GET / HTTP/1.1\r\n\r\n", 400),                            # HTTP/1.1 without Host
    (b"GET / HTTP/1.1\r\nHost: h\r\nBad Header: x\r\n\r\n", 400),
    (b"GET / HTTP/1.1\r\nHost : h\r\n\r\n", 400),                # space before colon (smuggling)
    (b"GET / HTTP/1.1\r\nHost: h\r\n folded\r\n\r\n", 400),     # obsolete line folding
    (b"GET / HTTP/1.1\r\nHost: h\r\nNoColon\r\n\r\n", 400),
    (b"GET / HTTP/1.1\r\nHost: h\r\n", 400),                     # connection closed mid-headers
    (b"GET /a%00b HTTP/1.1\r\nHost: h\r\n\r\n", 400),            # NUL byte in path
    (b"GET / HTTP/1.1", 400),                                    # incomplete request line
])
def test_malformed_requests(raw, status):
    assert status_of(raw) == status


@pytest.mark.parametrize("length_headers", [
    "Content-Length: 5\r\nContent-Length: 6\r\n",   # conflicting (request smuggling)
    "Content-Length: 5, 6\r\n",
    "Content-Length: -1\r\n",
    "Content-Length: abc\r\n",
])
def test_invalid_content_length(length_headers):
    assert status_of(f"POST / HTTP/1.1\r\nHost: h\r\n{length_headers}\r\nhello!".encode()) == 400


def test_repeated_identical_content_length_is_accepted():
    req = parse(b"POST / HTTP/1.1\r\nHost: h\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\nok")
    assert req.body == b"ok"


def test_transfer_encoding_is_refused_not_misread():
    raw = b"POST / HTTP/1.1\r\nHost: h\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n"
    assert status_of(raw) == 501


def test_truncated_body():
    assert status_of(b"POST / HTTP/1.1\r\nHost: h\r\nContent-Length: 10\r\n\r\nshort") == 400


def test_limits():
    assert status_of(b"GET /" + b"a" * 9000 + b" HTTP/1.1\r\n\r\n") == 414
    assert status_of(b"GET / HTTP/1.1\r\nHost: h\r\nX: " + b"a" * 9000 + b"\r\n\r\n") == 431
    many = "".join(f"X-{i}: v\r\n" for i in range(MAX_HEADERS + 1))
    assert status_of(f"GET / HTTP/1.1\r\nHost: h\r\n{many}\r\n".encode()) == 431
    assert status_of(f"POST / HTTP/1.1\r\nHost: h\r\nContent-Length: {MAX_BODY + 1}\r\n\r\n".encode()) == 413
