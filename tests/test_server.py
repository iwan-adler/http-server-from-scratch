"""Integration tests: a real server on a random port, real sockets."""

import http.client
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from minihttp.app import App
from minihttp.demo import build_app
from minihttp.response import Response
from minihttp.server import Server


@pytest.fixture
def server():
    srv = Server(build_app(), port=0, workers=8, idle_timeout=1.0, max_requests_per_connection=5)
    port = srv.start()
    yield port
    srv.shutdown()


def raw_exchange(port: int, data: bytes, read_timeout: float = 2.0) -> bytes:
    """Send raw bytes and read until the server closes the connection."""
    with socket.create_connection(("127.0.0.1", port), timeout=read_timeout) as sock:
        sock.sendall(data)
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except TimeoutError:
                break
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


def test_get_post_and_json(server):
    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=3)
    conn.request("GET", "/api/hello/Ada?greeting=Hi")
    r = conn.getresponse()
    assert r.status == 200 and json.loads(r.read()) == {"message": "Hi, Ada!"}

    conn.request("POST", "/api/echo", body=json.dumps({"x": [1, 2]}), headers={"Content-Type": "application/json"})
    r = conn.getresponse()
    assert json.loads(r.read())["you_sent"] == {"x": [1, 2]}

    conn.request("POST", "/api/echo", body="{not json", headers={"Content-Type": "application/json"})
    r = conn.getresponse()
    assert r.status == 400 and r.read()
    conn.request("POST", "/api/echo", body="x", headers={"Content-Type": "text/plain"})
    r = conn.getresponse()
    assert r.status == 415 and r.read()


def test_keep_alive_reuses_one_connection(server):
    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=3)
    ports = set()
    for _ in range(3):
        conn.request("GET", "/api/time")
        r = conn.getresponse()
        r.read()
        assert r.getheader("Connection") == "keep-alive"
        ports.add(conn.sock.getsockname()[1])
    assert len(ports) == 1  # same TCP connection every time


def test_pipelined_requests_are_answered_in_order(server):
    raw = (b"GET /api/hello/one HTTP/1.1\r\nHost: t\r\n\r\n"
           b"GET /api/hello/two HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n")
    reply = raw_exchange(server, raw)
    assert reply.count(b"HTTP/1.1 200 OK") == 2
    assert reply.index(b"Hello, one!") < reply.index(b"Hello, two!")


def test_http_10_closes_after_one_response(server):
    reply = raw_exchange(server, b"GET /api/time HTTP/1.0\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 200 OK") and b"Connection: close" in reply


def test_malformed_request_gets_400_and_connection_closes(server):
    reply = raw_exchange(server, b"THIS IS NOT HTTP\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 400 Bad Request") and b"Connection: close" in reply


def test_smuggling_attempt_is_rejected(server):
    reply = raw_exchange(server, b"POST /api/echo HTTP/1.1\r\nHost: t\r\nContent-Length: 4\r\n"
                                 b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 501")


def test_path_traversal_over_the_wire(server):
    reply = raw_exchange(server, b"GET /../minihttp/server.py HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 404") and b"import socket" not in reply


def test_head_has_length_but_no_body(server):
    reply = raw_exchange(server, b"HEAD / HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n")
    head, _, body = reply.partition(b"\r\n\r\n")
    assert b"Content-Length: " in head and body == b""


def test_handler_crash_returns_500_and_server_survives(server):
    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=3)
    conn.request("GET", "/api/boom")
    r = conn.getresponse()
    assert r.status == 500 and b"Traceback" not in r.read()
    conn.request("GET", "/api/time")
    assert conn.getresponse().status == 200


def test_idle_connection_is_closed_after_timeout(server):
    with socket.create_connection(("127.0.0.1", server), timeout=5) as sock:
        started = time.monotonic()
        assert sock.recv(1) == b""  # server hangs up after idle_timeout (1 s) without a request
        assert 0.5 < time.monotonic() - started < 4


def test_max_requests_per_connection(server):
    raw = b"".join(b"GET /api/time HTTP/1.1\r\nHost: t\r\n\r\n" for _ in range(7))
    reply = raw_exchange(server, raw)
    assert reply.count(b"HTTP/1.1 200 OK") == 5  # limit is 5 per connection
    assert reply.count(b"Connection: close") == 1


def test_concurrent_slow_requests_run_in_parallel():
    app = App()

    @app.route("/slow")
    def slow(request):
        time.sleep(0.5)
        return Response.text("done")

    srv = Server(app, port=0, workers=8)
    port = srv.start()
    try:
        def fetch(_):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/slow")
            return conn.getresponse().status

        started = time.monotonic()
        with ThreadPoolExecutor(8) as pool:
            statuses = list(pool.map(fetch, range(8)))
        elapsed = time.monotonic() - started
    finally:
        srv.shutdown()
    assert statuses == [200] * 8
    assert elapsed < 2.0  # 8 x 0.5 s sequentially would take 4 s


def test_shutdown_finishes_in_flight_request():
    app = App()
    release = threading.Event()

    @app.route("/wait")
    def wait(request):
        release.wait(5)
        return Response.text("finished")

    srv = Server(app, port=0, workers=2)
    port = srv.start()
    result = {}

    def client():
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/wait")
        response = conn.getresponse()
        result["status"], result["body"] = response.status, response.read()

    thread = threading.Thread(target=client)
    thread.start()
    time.sleep(0.3)
    stopper = threading.Thread(target=srv.shutdown)
    stopper.start()
    time.sleep(0.2)
    release.set()
    thread.join(5)
    stopper.join(5)
    assert result == {"status": 200, "body": b"finished"}
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
