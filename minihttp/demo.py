"""Demo app. Run: python -m minihttp.demo [--port 8080]"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from minihttp.app import App
from minihttp.request import Request
from minihttp.response import Response
from minihttp.server import Server

PUBLIC = Path(__file__).resolve().parent.parent / "public"


def build_app() -> App:
    app = App()

    @app.route("/api/time")
    def now(request: Request) -> Response:
        return Response.json({"utc": datetime.now(timezone.utc).isoformat(timespec="seconds")})

    @app.route("/api/hello/{name}")
    def hello(request: Request) -> Response:
        greeting = request.query.get("greeting", ["Hello"])[0]
        return Response.json({"message": f"{greeting}, {request.params['name']}!"})

    @app.route("/api/echo", methods=("POST",))
    def echo(request: Request) -> Response:
        if request.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return Response.error(415, "Send Content-Type: application/json")
        try:
            data = json.loads(request.body or b"null")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return Response.error(400, "Body is not valid JSON")
        return Response.json({"you_sent": data, "bytes": len(request.body)})

    @app.route("/api/boom")
    def boom(request: Request) -> Response:
        raise RuntimeError("deliberate failure to show 500 handling")

    app.static("/", PUBLIC)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="minihttp demo server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--workers", type=int, default=32)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    server = Server(build_app(), host=args.host, port=args.port, workers=args.workers)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
