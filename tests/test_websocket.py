"""WebSocket protocol tests: handshake, framing, protocol violations, close handshake, heartbeat.

A raw-socket client is used where we must send *invalid* frames (well-behaved
libraries refuse to); the independent `websockets` library is used for normal traffic.
"""

import base64
import io
import os
import socket
import struct
import threading
import time

import pytest
from websockets.sync.client import connect

from minihttp import websocket as ws_mod
from minihttp.app import App
from minihttp.server import Server
from minihttp.websocket import BINARY, CLOSE, CONTINUATION, PING, PONG, TEXT, accept_key, encode_frame, read_frame


def echo_app() -> App:
    app = App()

    @app.websocket("/echo")
    def echo(ws, request):
        while (message := ws.recv()) is not None:
            ws.send_text(message) if isinstance(message, str) else ws.send_bytes(message)

    @app.websocket("/crash")
    def crash(ws, request):
        raise RuntimeError("handler bug")

    return app


@pytest.fixture
def server():
    srv = Server(echo_app(), port=0, workers=32, websocket_heartbeat=0.4)
    port = srv.start()
    yield port
    srv.shutdown()


class RawClient:
    """Minimal WebSocket client that can also misbehave on purpose."""

    def __init__(self, port: int, path: str = "/echo") -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=3)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: t\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        self.buffer = b""
        head = self._until(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 101"), head
        assert f"Sec-WebSocket-Accept: {accept_key(key)}".encode() in head

    def _until(self, marker: bytes) -> bytes:
        while marker not in self.buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buffer += chunk
        head, _, self.buffer = self.buffer.partition(marker)
        return head

    def _exact(self, n: int) -> bytes:
        while len(self.buffer) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buffer += chunk
        data, self.buffer = self.buffer[:n], self.buffer[n:]
        return data

    def send(self, opcode: int, payload: bytes, fin: bool = True, masked: bool = True, first_byte: int | None = None):
        frame = bytearray(encode_frame(opcode, payload, fin=fin, mask=os.urandom(4) if masked else None))
        if first_byte is not None:
            frame[0] = first_byte
        self.sock.sendall(bytes(frame))

    def recv(self) -> tuple[int, bytes]:
        first, second = self._exact(2)
        assert not second & 0x80, "server frames must not be masked"
        length = second & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self._exact(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._exact(8))
        return first & 0x0F, self._exact(length)

    def expect_close(self, code: int) -> None:
        opcode, payload = self.recv()
        while opcode == PING:  # heartbeat pings may arrive first
            opcode, payload = self.recv()
        assert opcode == CLOSE, (opcode, payload)
        assert struct.unpack("!H", payload[:2])[0] == code, payload


# --- handshake ------------------------------------------------------------------------------------


def test_accept_key_matches_rfc_6455_example():
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


@pytest.mark.parametrize(("headers", "status"), [
    ("Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n", b"400"),  # no key
    ("Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: c2hvcnQ=\r\nSec-WebSocket-Version: 13\r\n", b"400"),
    ("Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 8\r\n", b"426"),
    ("", b"400"),  # a plain GET on a WebSocket route
])
def test_bad_handshakes(server, headers, status):
    with socket.create_connection(("127.0.0.1", server), timeout=3) as sock:
        sock.sendall(f"GET /echo HTTP/1.1\r\nHost: t\r\n{headers}\r\n".encode())
        reply = sock.recv(4096)
    assert reply.startswith(b"HTTP/1.1 " + status)
    if status == b"426":
        assert b"Sec-WebSocket-Version: 13" in reply


# --- framing ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(("size", "header"), [(0, 2), (125, 2), (126, 4), (65535, 4), (65536, 10)])
def test_length_encodings_round_trip(size, header):
    payload = os.urandom(size)
    assert len(encode_frame(BINARY, payload)) == header + size
    masked = encode_frame(BINARY, payload, mask=b"\x01\x02\x03\x04")
    assert read_frame(io.BytesIO(masked), max_size=1 << 20) == (True, BINARY, payload)


def test_echo_with_an_independent_client(server):
    with connect(f"ws://127.0.0.1:{server}/echo") as client:
        client.send("héllo 👋")
        assert client.recv() == "héllo 👋"
        client.send(b"\x00\xff" * 1000)
        assert client.recv() == b"\x00\xff" * 1000
        assert client.ping().wait(2)


def test_fragmented_message_with_a_ping_in_the_middle(server):
    c = RawClient(server)
    c.send(TEXT, b"Hel", fin=False)
    c.send(PING, b"are you there")
    c.send(CONTINUATION, b"lo ", fin=False)
    c.send(CONTINUATION, b"world")
    assert c.recv() == (PONG, b"are you there")  # control frames are answered immediately
    assert c.recv() == (TEXT, b"Hello world")


@pytest.mark.parametrize(("make", "code"), [
    (lambda c: c.send(TEXT, b"hi", masked=False), 1002),                      # client frames must be masked
    (lambda c: c.send(TEXT, b"hi", first_byte=0x80 | 0x40 | TEXT), 1002),     # RSV1 set
    (lambda c: c.send(0x3, b"hi"), 1002),                                     # reserved opcode
    (lambda c: c.send(PING, b"x" * 126), 1002),                               # control frame too long
    (lambda c: c.send(PING, b"x", fin=False), 1002),                          # fragmented control frame
    (lambda c: c.send(CONTINUATION, b"orphan"), 1002),                        # continuation with no message
    (lambda c: (c.send(TEXT, b"a", fin=False), c.send(TEXT, b"b")), 1002),    # new message mid-message
    (lambda c: c.send(TEXT, b"\xff\xfe"), 1007),                              # invalid UTF-8
    (lambda c: c.send(BINARY, b"x" * 70000), 1009),                           # over 64 KB
    (lambda c: c.send(CLOSE, b"\x03"), 1002),                                 # 1-byte close payload
    (lambda c: c.send(CLOSE, struct.pack("!H", 999)), 1002),                  # invalid close code
])
def test_protocol_violations_get_the_right_close_code(server, make, code):
    c = RawClient(server)
    make(c)
    c.expect_close(code)


def test_close_handshake_echoes_the_code(server):
    c = RawClient(server)
    c.send(CLOSE, struct.pack("!H", 1000) + "bye".encode())
    c.expect_close(1000)
    with pytest.raises((ConnectionError, OSError)):
        c.recv()  # the server hangs up after the close handshake


def test_heartbeat_ping_then_disconnect_silent_client(server):
    c = RawClient(server)
    started = time.monotonic()
    opcode, payload = c.recv()  # heartbeat = 0.4 s
    assert (opcode, payload) == (PING, b"heartbeat")
    c.expect_close(1001)        # no pong within another 0.4 s: considered dead
    assert time.monotonic() - started < 3


def test_answering_pings_keeps_the_connection_alive(server):
    c = RawClient(server)
    for _ in range(3):
        opcode, payload = c.recv()
        assert opcode == PING
        c.send(PONG, payload)
    c.send(TEXT, b"still here")
    assert c.recv() == (TEXT, b"still here")


def test_handler_crash_closes_with_1011(server):
    RawClient(server, "/crash").expect_close(1011)


def test_many_concurrent_connections(server):
    results = []

    def talk(i):
        with connect(f"ws://127.0.0.1:{server}/echo") as client:
            for j in range(20):
                client.send(f"{i}-{j}")
                assert client.recv() == f"{i}-{j}"
        results.append(i)

    threads = [threading.Thread(target=talk, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(results) == list(range(20))


def test_plain_http_still_works_next_to_websockets(server):
    with socket.create_connection(("127.0.0.1", server), timeout=3) as sock:
        sock.sendall(b"GET /nothing HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n")
        assert sock.recv(4096).startswith(b"HTTP/1.1 404")


def test_close_frame_values():
    assert ws_mod.VALID_CLOSE_CODES >= {1000, 1001, 1002, 1003, 1007, 1008, 1009, 1011, 3000, 4999}
    assert 1004 not in ws_mod.VALID_CLOSE_CODES and 1005 not in ws_mod.VALID_CLOSE_CODES


def test_shutdown_closes_open_websockets_instead_of_hanging():
    srv = Server(echo_app(), port=0, websocket_heartbeat=30)  # heartbeat far longer than the test
    port = srv.start()
    c = RawClient(port)
    started = time.monotonic()
    srv.shutdown()
    assert time.monotonic() - started < 3
    c.expect_close(1001)  # Going Away
