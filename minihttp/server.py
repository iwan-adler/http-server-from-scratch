"""TCP server: accept connections, parse requests, run the app, write responses.

Concurrency model: one accept loop plus a bounded pool of worker threads, one
connection per worker at a time. Simple, and enough for blocking handlers; the
trade-off is that idle keep-alive connections each occupy a worker until the
idle timeout, so the timeout is kept short.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from minihttp.request import HTTPError, read_request
from minihttp.response import Response, serialize

logger = logging.getLogger("minihttp")


class Server:
    def __init__(
        self,
        app,
        host: str = "127.0.0.1",
        port: int = 8080,
        workers: int = 32,
        idle_timeout: float = 5.0,
        max_requests_per_connection: int = 100,
    ) -> None:
        self.app = app
        self.host, self.port = host, port
        self.idle_timeout = idle_timeout
        self.max_requests = max_requests_per_connection
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="minihttp")
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    # --- lifecycle ---------------------------------------------------------------

    def bind(self) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Lets a restarted server reuse the port immediately (not on Windows, where it
        # would allow two servers on one port).
        if hasattr(socket, "SO_REUSEADDR") and not hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(128)
        sock.settimeout(0.2)  # wake up regularly to notice shutdown()
        self._sock = sock
        self.port = sock.getsockname()[1]  # the real port when 0 was requested
        return self.port

    def serve_forever(self) -> None:
        if self._sock is None:
            self.bind()
        logger.info("listening on http://%s:%d", self.host, self.port)
        while not self._stop.is_set():
            try:
                conn, address = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                raise
            self._pool.submit(self._handle_connection, conn, address)

    def start(self) -> int:
        """Run in a background thread (used by tests and the benchmark). Returns the port."""
        port = self.bind()
        self._thread = threading.Thread(target=self.serve_forever, name="minihttp-accept", daemon=True)
        self._thread.start()
        return port

    def shutdown(self) -> None:
        """Stop accepting, let in-flight requests finish, then close."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._pool.shutdown(wait=True, cancel_futures=True)
        if self._sock:
            self._sock.close()

    # --- one connection ------------------------------------------------------------

    def _handle_connection(self, conn: socket.socket, address: tuple) -> None:
        conn.settimeout(self.idle_timeout)
        stream = conn.makefile("rb")
        try:
            for served in range(1, self.max_requests + 1):
                if self._stop.is_set():
                    break
                try:
                    request = read_request(stream)
                except HTTPError as error:
                    # The stream position is unknown after a bad request, so always close.
                    conn.sendall(serialize(Response.error(error.status, error.message), keep_alive=False))
                    self._log(address, "-", error.status, 0, 0.0)
                    break
                if request is None:
                    break  # client closed the connection
                started = time.perf_counter()
                response = self._run_app(request)
                keep_alive = request.keep_alive and served < self.max_requests and not self._stop.is_set()
                payload = serialize(response, keep_alive=keep_alive, head_only=request.method == "HEAD")
                conn.sendall(payload)
                self._log(address, f"{request.method} {request.target} {request.version}",
                          response.status, len(response.body), (time.perf_counter() - started) * 1000)
                if not keep_alive:
                    break
        except (TimeoutError, ConnectionError):
            pass  # idle keep-alive connection timed out, or the client went away
        finally:
            stream.close()
            try:
                conn.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            conn.close()

    def _run_app(self, request) -> Response:
        try:
            response = self.app(request)
            if not isinstance(response, Response):
                raise TypeError(f"handler returned {type(response).__name__}, not Response")
            return response
        except Exception:
            # A bug in a handler must not kill the worker or leak a stack trace to the client.
            logger.exception("handler failed for %s %s", request.method, request.path)
            return Response.error(500, "Internal server error")

    @staticmethod
    def _log(address: tuple, request_line: str, status: int, size: int, ms: float) -> None:
        logger.info('%s "%s" %d %d %.1fms', address[0], request_line, status, size, ms)
