"""Throughput/latency comparison: minihttp vs. Python's built-in ThreadingHTTPServer.

Usage: python bench/bench.py [--clients 16] [--requests 500]

Both servers return the same small JSON body on GET /api/time, run in this
process on 127.0.0.1, and are hit by keep-alive clients (one thread each).
Numbers depend heavily on the machine; compare them only with each other.
"""

from __future__ import annotations

import argparse
import http.client
import json
import statistics
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minihttp.app import App  # noqa: E402
from minihttp.response import Response  # noqa: E402
from minihttp.server import Server  # noqa: E402

BODY = json.dumps({"utc": "2026-01-01T00:00:00+00:00"}).encode()


def start_minihttp(workers: int) -> tuple[int, callable]:
    app = App()

    @app.route("/api/time")
    def now(request):
        return Response(200, BODY, {"Content-Type": "application/json"})

    server = Server(app, port=0, workers=workers, max_requests_per_connection=10_000)
    return server.start(), server.shutdown


def start_stdlib() -> tuple[int, callable]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"  # enables keep-alive

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def stop():
        server.shutdown()
        server.server_close()

    return server.server_address[1], stop


def load(port: int, clients: int, per_client: int) -> dict:
    latencies: list[float] = []
    errors = 0
    lock = threading.Lock()

    def client() -> None:
        nonlocal errors
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        mine = []
        for _ in range(per_client):
            started = time.perf_counter()
            try:
                conn.request("GET", "/api/time")
                response = conn.getresponse()
                if response.read() != BODY or response.status != 200:
                    raise ValueError("bad response")
            except Exception:
                with lock:
                    errors += 1
                conn.close()
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                continue
            mine.append(time.perf_counter() - started)
        conn.close()
        with lock:
            latencies.extend(mine)

    threads = [threading.Thread(target=client) for _ in range(clients)]
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - started
    ms = sorted(x * 1000 for x in latencies)
    pct = lambda p: ms[min(len(ms) - 1, int(len(ms) * p))]  # noqa: E731
    return {
        "requests": len(ms), "errors": errors, "seconds": round(elapsed, 2),
        "req_per_s": round(len(ms) / elapsed), "p50_ms": round(statistics.median(ms), 2),
        "p95_ms": round(pct(0.95), 2), "p99_ms": round(pct(0.99), 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clients", type=int, default=16)
    parser.add_argument("--requests", type=int, default=500, help="requests per client")
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()

    results = {}
    for name, start in (("minihttp", lambda: start_minihttp(args.clients)), ("stdlib ThreadingHTTPServer", start_stdlib)):
        port, stop = start()
        try:
            load(port, 4, 50)  # warm-up
            runs = [load(port, args.clients, args.requests) for _ in range(args.rounds)]
        finally:
            stop()
        best = max(runs, key=lambda r: r["req_per_s"])
        results[name] = {**best, "req_per_s_all_rounds": [r["req_per_s"] for r in runs]}

    print(f"{args.clients} keep-alive clients x {args.requests} requests, best of {args.rounds} rounds\n")
    print(f"{'server':28} {'req/s':>8} {'p50 ms':>8} {'p95 ms':>8} {'p99 ms':>8} {'errors':>7}  all rounds")
    for name, r in results.items():
        print(f"{name:28} {r['req_per_s']:>8} {r['p50_ms']:>8} {r['p95_ms']:>8} {r['p99_ms']:>8} {r['errors']:>7}  {r['req_per_s_all_rounds']}")


if __name__ == "__main__":
    main()
