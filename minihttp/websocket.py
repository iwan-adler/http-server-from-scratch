"""WebSocket protocol (RFC 6455): opening handshake, framing, close handshake, heartbeat.

A WebSocket starts as an HTTP/1.1 GET with `Upgrade: websocket`. The server
replies 101 Switching Protocols, and from then on both sides exchange *frames*
over the same TCP connection:

    byte 0: FIN | RSV1-3 | opcode (4 bits)
    byte 1: MASK | payload length (7 bits; 126 -> next 2 bytes, 127 -> next 8 bytes)
    [4-byte masking key, present on every client-to-server frame]
    payload (client payloads are XOR-ed with the masking key)
"""

from __future__ import annotations

import base64
import hashlib
import select
import socket
import struct
import threading
import time
from typing import BinaryIO

from minihttp.request import Request
from minihttp.response import Response

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # fixed by RFC 6455

CONTINUATION, TEXT, BINARY, CLOSE, PING, PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
DATA_OPCODES = {CONTINUATION, TEXT, BINARY}
CONTROL_OPCODES = {CLOSE, PING, PONG}

# Close codes (RFC 6455 §7.4.1)
NORMAL, GOING_AWAY, PROTOCOL_ERROR, UNSUPPORTED_DATA = 1000, 1001, 1002, 1003
INVALID_PAYLOAD, POLICY_VIOLATION, TOO_BIG, INTERNAL_ERROR = 1007, 1008, 1009, 1011
VALID_CLOSE_CODES = {1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011} | set(range(3000, 5000))

MAX_MESSAGE = 64 * 1024


class WebSocketClosed(Exception):
    """Raised by send() after the connection has closed."""


class _ProtocolViolation(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code, self.reason = code, reason


# --- opening handshake ---------------------------------------------------------------------------


def accept_key(client_key: str) -> str:
    """Sec-WebSocket-Accept = base64(SHA-1(key + GUID)). Proves the server understood
    the WebSocket request (so a plain HTTP server can't be tricked into one)."""
    return base64.b64encode(hashlib.sha1((client_key + GUID).encode()).digest()).decode()


def is_upgrade(request: Request) -> bool:
    connection = {t.strip().lower() for v in request.headers.get_all("Connection") for t in v.split(",")}
    return "upgrade" in connection and (request.headers.get("Upgrade") or "").lower() == "websocket"


def handshake(request: Request) -> Response:
    """Validate an upgrade request. Returns the 101 response, or an error response."""
    if request.method != "GET" or request.version != "HTTP/1.1":
        return Response.error(400, "WebSocket upgrades must use GET over HTTP/1.1")
    if not is_upgrade(request):
        return Response.error(400, "Missing 'Connection: Upgrade' / 'Upgrade: websocket'")
    if request.headers.get("Sec-WebSocket-Version") != "13":
        response = Response.error(426, "Only WebSocket version 13 is supported")
        response.headers["Sec-WebSocket-Version"] = "13"
        return response
    key = request.headers.get("Sec-WebSocket-Key") or ""
    try:
        valid_key = len(base64.b64decode(key, validate=True)) == 16
    except ValueError:
        valid_key = False
    if not valid_key:
        return Response.error(400, "Sec-WebSocket-Key must be 16 random bytes, base64-encoded")
    return Response(101, b"", {"Upgrade": "websocket", "Connection": "Upgrade",
                               "Sec-WebSocket-Accept": accept_key(key)})


def handshake_bytes(response: Response) -> bytes:
    """101 has no body and must not carry Content-Length; build it directly."""
    head = "HTTP/1.1 101 Switching Protocols\r\n" + "".join(f"{k}: {v}\r\n" for k, v in response.headers.items())
    return (head + "\r\n").encode("latin-1")


# --- frames -----------------------------------------------------------------------------------------


def encode_frame(opcode: int, payload: bytes, fin: bool = True, mask: bytes | None = None) -> bytes:
    """Build a frame. Servers never mask; tests pass `mask` to act as a client."""
    first = (0x80 if fin else 0) | opcode
    length = len(payload)
    mask_bit = 0x80 if mask else 0
    if length < 126:
        header = struct.pack("!BB", first, mask_bit | length)
    elif length < 1 << 16:
        header = struct.pack("!BBH", first, mask_bit | 126, length)
    else:
        header = struct.pack("!BBQ", first, mask_bit | 127, length)
    if mask:
        return header + mask + _xor(payload, mask)
    return header + payload


def _xor(data: bytes, key: bytes) -> bytes:
    # XOR-ing whole integers is far faster in Python than byte by byte.
    repeated = (key * (len(data) // 4 + 1))[: len(data)]
    return (int.from_bytes(data, "big") ^ int.from_bytes(repeated, "big")).to_bytes(len(data), "big")


def _read_exact(stream: BinaryIO, n: int) -> bytes:
    data = stream.read(n)
    if len(data) != n:
        raise ConnectionError("connection closed mid-frame")
    return data


def read_frame(stream: BinaryIO, max_size: int = MAX_MESSAGE) -> tuple[bool, int, bytes]:
    """Read one client frame. Returns (fin, opcode, unmasked payload)."""
    first, second = _read_exact(stream, 2)
    fin, rsv, opcode = bool(first & 0x80), first & 0x70, first & 0x0F
    masked, length = bool(second & 0x80), second & 0x7F
    if rsv:
        raise _ProtocolViolation(PROTOCOL_ERROR, "reserved bits set (no extensions were negotiated)")
    if opcode not in DATA_OPCODES | CONTROL_OPCODES:
        raise _ProtocolViolation(PROTOCOL_ERROR, f"unknown opcode {opcode:#x}")
    if not masked:
        raise _ProtocolViolation(PROTOCOL_ERROR, "client frames must be masked")
    if opcode in CONTROL_OPCODES and (not fin or length > 125):
        raise _ProtocolViolation(PROTOCOL_ERROR, "control frames must be unfragmented and at most 125 bytes")
    if length == 126:
        (length,) = struct.unpack("!H", _read_exact(stream, 2))
    elif length == 127:
        (length,) = struct.unpack("!Q", _read_exact(stream, 8))
    if length > max_size:
        raise _ProtocolViolation(TOO_BIG, f"frame of {length} bytes exceeds {max_size}")
    key = _read_exact(stream, 4)
    return fin, opcode, _xor(_read_exact(stream, length), key)


# --- connection --------------------------------------------------------------------------------------


class SocketReader:
    """Reads exact byte counts from a raw socket through a small buffer.

    Python's buffered socket file (sock.makefile) refuses every read after one
    times out, which would make a heartbeat impossible. Waiting with select()
    and reading with recv() keeps the socket usable after an idle period.
    """

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buffer = bytearray()

    def wait(self, timeout: float) -> bool:
        """True when data is available (or buffered) within `timeout` seconds."""
        if self.buffer:
            return True
        readable, _, _ = select.select([self.sock], [], [], timeout)
        return bool(readable)

    def read(self, n: int) -> bytes:
        while len(self.buffer) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buffer)))
            if not chunk:
                raise ConnectionError("connection closed")
            self.buffer += chunk
        data = bytes(self.buffer[:n])
        del self.buffer[:n]
        return data


class WebSocket:
    """A server-side WebSocket on an upgraded connection.

    recv() returns str (text), bytes (binary) or None once the connection is closed.
    It answers pings, enforces the protocol, and sends a heartbeat ping when the
    client has been silent for `heartbeat` seconds; if that ping gets no reply
    within another `heartbeat` seconds, the connection is considered dead.
    send_*() may be called from other threads (e.g. to broadcast).
    """

    def __init__(self, sock: socket.socket, heartbeat: float = 20.0, max_message: int = MAX_MESSAGE) -> None:
        self.sock = sock
        self._reader = SocketReader(sock)
        self.max_message = max_message
        self.heartbeat = heartbeat
        self.closed = False
        self.close_code: int | None = None
        self._send_lock = threading.Lock()
        # Once a frame has started, the rest must arrive promptly; a client stalling
        # mid-frame (slowloris-style) hits this timeout and is disconnected.
        sock.settimeout(heartbeat)

    # sending
    def _send(self, opcode: int, payload: bytes) -> None:
        with self._send_lock:
            if self.closed:
                raise WebSocketClosed("connection is closed")
            try:
                self.sock.sendall(encode_frame(opcode, payload))
            except OSError as exc:
                self.closed = True
                raise WebSocketClosed(str(exc)) from exc

    def send_text(self, text: str) -> None:
        self._send(TEXT, text.encode("utf-8"))

    def send_bytes(self, data: bytes) -> None:
        self._send(BINARY, data)

    def ping(self, payload: bytes = b"") -> None:
        self._send(PING, payload)

    def close(self, code: int = NORMAL, reason: str = "") -> None:
        """Send a close frame (the client should answer with its own and hang up)."""
        if self.closed:
            return
        try:
            self._send(CLOSE, struct.pack("!H", code) + reason.encode("utf-8")[:123])
        except WebSocketClosed:
            pass
        self.closed = True
        self.close_code = code

    # receiving
    def recv(self) -> str | bytes | None:
        fragments: list[bytes] = []
        message_opcode: int | None = None
        awaiting_pong = False
        while not self.closed:
            try:
                if not self._wait_for_data():
                    if self.closed:
                        return None  # closed by another thread (e.g. server shutdown)
                    if awaiting_pong:
                        self.close(GOING_AWAY, "heartbeat timeout")
                        return None
                    awaiting_pong = True
                    self.ping(b"heartbeat")
                    continue
                fin, opcode, payload = read_frame(self._reader, self.max_message)
            except WebSocketClosed:
                return None
            except _ProtocolViolation as violation:
                self.close(violation.code, violation.reason)
                return None
            except (ConnectionError, OSError):
                self.closed = True
                return None
            awaiting_pong = False  # any frame proves the client is alive

            if opcode == PING:
                self._send(PONG, payload)  # must echo the ping's payload
                continue
            if opcode == PONG:
                continue
            if opcode == CLOSE:
                self._on_close(payload)
                return None

            # data frames, possibly fragmented (control frames may arrive in between)
            if opcode == CONTINUATION:
                if message_opcode is None:
                    self.close(PROTOCOL_ERROR, "continuation frame without a message")
                    return None
            else:
                if message_opcode is not None:
                    self.close(PROTOCOL_ERROR, "new message before the previous one finished")
                    return None
                message_opcode = opcode
            fragments.append(payload)
            if sum(len(f) for f in fragments) > self.max_message:
                self.close(TOO_BIG, "message too big")
                return None
            if fin:
                data = b"".join(fragments)
                if message_opcode == BINARY:
                    return data
                try:
                    return data.decode("utf-8")
                except UnicodeDecodeError:
                    self.close(INVALID_PAYLOAD, "text message is not valid UTF-8")
                    return None
        return None

    def _wait_for_data(self) -> bool:
        """Wait up to `heartbeat` seconds for data, in short slices so that a close()
        from another thread is noticed quickly (closing or shutting down a socket from
        another thread does not reliably wake select() on every platform, e.g. Windows)."""
        deadline = time.monotonic() + self.heartbeat
        while not self.closed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self._reader.wait(min(0.25, remaining)):
                return True
        return False

    def _on_close(self, payload: bytes) -> None:
        code = NORMAL
        if len(payload) == 1:
            code = PROTOCOL_ERROR
        elif len(payload) >= 2:
            (code,) = struct.unpack("!H", payload[:2])
            try:
                payload[2:].decode("utf-8")
            except UnicodeDecodeError:
                code = INVALID_PAYLOAD
            if code not in VALID_CLOSE_CODES:
                code = PROTOCOL_ERROR
        # Echo the client's code back (or report its mistake), completing the close handshake.
        self.close(code if code in (PROTOCOL_ERROR, INVALID_PAYLOAD) else (code if payload else NORMAL))
        self.close_code = code
