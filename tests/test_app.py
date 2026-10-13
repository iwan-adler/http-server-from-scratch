import io
import os
import time
from email.utils import formatdate

import pytest

from minihttp.app import App, compile_path
from minihttp.request import read_request
from minihttp.response import Response, serialize


def req(method: str, target: str, headers: str = "") -> object:
    return read_request(io.BytesIO(f"{method} {target} HTTP/1.1\r\nHost: h\r\n{headers}\r\n".encode()))


@pytest.fixture
def site(tmp_path):
    (tmp_path / "public").mkdir()
    (tmp_path / "public" / "index.html").write_text("<h1>home</h1>", encoding="utf-8")
    (tmp_path / "public" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "public" / "docs").mkdir()
    (tmp_path / "public" / "docs" / "index.html").write_text("docs", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("TOP SECRET", encoding="utf-8")  # outside the static root
    app = App()

    @app.route("/users/{user_id}/posts/{post_id}")
    def post(request):
        return Response.json(request.params)

    @app.route("/items", methods=("POST",))
    def create(request):
        return Response.json({"created": True}, status=201)

    app.static("/", tmp_path / "public")
    return app


def test_path_parameters(site):
    assert site(req("GET", "/users/7/posts/abc")).body == b'{"user_id": "7", "post_id": "abc"}'
    assert compile_path("/a.b/{x}").match("/a.b/1") and not compile_path("/a.b/{x}").match("/aXb/1")


def test_method_not_allowed_lists_allowed_methods(site):
    response = site(req("GET", "/items"))
    assert response.status == 405 and response.headers["Allow"] == "POST"
    assert site(req("POST", "/items")).status == 201
    assert site(req("HEAD", "/users/1/posts/2")).status == 200  # HEAD allowed wherever GET is


def test_static_files_and_index(site):
    home = site(req("GET", "/"))
    assert home.status == 200 and home.body == b"<h1>home</h1>"
    assert home.headers["Content-Type"] == "text/html; charset=utf-8"
    assert site(req("GET", "/docs/")).body == b"docs"
    js = site(req("GET", "/app.js"))
    assert js.headers["Content-Type"].startswith(("text/javascript", "application/javascript"))
    assert site(req("GET", "/missing.png")).status == 404
    assert site(req("POST", "/app.js")).status == 405


@pytest.mark.parametrize("target", [
    "/../secret.txt", "/%2e%2e/secret.txt", "/docs/../../secret.txt", "/..%2fsecret.txt",
    "/%2e%2e%5csecret.txt", "/C:%5cWindows%5cwin.ini",
])
def test_path_traversal_is_blocked(site, target):
    response = site(req("GET", target))
    assert response.status == 404
    assert b"TOP SECRET" not in response.body


def test_conditional_get_returns_304(site, tmp_path):
    path = tmp_path / "public" / "app.js"
    stamp = time.time() - 100
    os.utime(path, (stamp, stamp))
    fresh = formatdate(stamp + 50, usegmt=True)
    stale = formatdate(stamp - 50, usegmt=True)
    assert site(req("GET", "/app.js", f"If-Modified-Since: {fresh}\r\n")).status == 304
    assert site(req("GET", "/app.js", f"If-Modified-Since: {stale}\r\n")).status == 200
    assert site(req("GET", "/app.js", "If-Modified-Since: garbage\r\n")).status == 200


# --- response serialisation -----------------------------------------------------------------


def test_serialize_sets_length_and_connection():
    raw = serialize(Response.text("héllo"), keep_alive=False)
    head, body = raw.split(b"\r\n\r\n", 1)
    assert head.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"Content-Length: 6" in head and b"Connection: close" in head and b"Date: " in head
    assert body == "héllo".encode()


def test_head_and_304_send_no_body():
    head_only = serialize(Response.text("abc"), keep_alive=True, head_only=True)
    assert head_only.endswith(b"\r\n\r\n") and b"Content-Length: 3" in head_only
    not_modified = serialize(Response(304, b"ignored"), keep_alive=True)
    assert not_modified.endswith(b"\r\n\r\n") and b"Content-Length" not in not_modified


def test_header_injection_is_refused():
    with pytest.raises(ValueError):
        serialize(Response(200, b"", {"X-Name": "a\r\nSet-Cookie: evil=1"}), keep_alive=True)
