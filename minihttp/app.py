"""Routing and static files: turns a Request into a Response."""

from __future__ import annotations

import mimetypes
import re
from collections.abc import Callable
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path

from minihttp.request import Request
from minihttp.response import Response

Handler = Callable[[Request], Response]
PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def compile_path(pattern: str) -> re.Pattern[str]:
    """'/hello/{name}' -> a regex where {name} matches one path segment."""
    regex, last = "", 0
    for match in PARAM.finditer(pattern):
        regex += re.escape(pattern[last:match.start()]) + f"(?P<{match.group(1)}>[^/]+)"
        last = match.end()
    return re.compile("^" + regex + re.escape(pattern[last:]) + "$")


class App:
    def __init__(self) -> None:
        self._routes: list[tuple[re.Pattern[str], set[str], Handler]] = []
        self._static: list[tuple[str, Path]] = []

    def route(self, pattern: str, methods: tuple[str, ...] = ("GET",)) -> Callable[[Handler], Handler]:
        def register(handler: Handler) -> Handler:
            allowed = set(methods)
            if "GET" in allowed:
                allowed.add("HEAD")  # HEAD is GET without the body; the server drops it
            self._routes.append((compile_path(pattern), allowed, handler))
            return handler
        return register

    def static(self, prefix: str, directory: str | Path) -> None:
        self._static.append((prefix.rstrip("/") + "/", Path(directory).resolve()))

    def __call__(self, request: Request) -> Response:
        allowed_here: set[str] = set()
        for pattern, methods, handler in self._routes:
            match = pattern.match(request.path)
            if not match:
                continue
            if request.method in methods:
                request.params = match.groupdict()
                return handler(request)
            allowed_here |= methods
        if allowed_here:
            response = Response.error(405, f"{request.method} is not allowed here")
            response.headers["Allow"] = ", ".join(sorted(allowed_here))
            return response
        for prefix, root in self._static:
            if request.path.startswith(prefix) or request.path == prefix.rstrip("/"):
                if request.method not in ("GET", "HEAD"):
                    response = Response.error(405, "Static files are read-only")
                    response.headers["Allow"] = "GET, HEAD"
                    return response
                return serve_file(root, request.path[len(prefix):], request)
        return Response.error(404, f"No route for {request.path}")


def serve_file(root: Path, relative: str, request: Request) -> Response:
    """Serve root/relative, refusing anything that resolves outside root."""
    candidate = (root / relative.lstrip("/")).resolve()
    if not candidate.is_relative_to(root):  # blocks ../ and absolute-path tricks
        return Response.error(404, "Not found")
    if candidate.is_dir():
        candidate = candidate / "index.html"
    if not candidate.is_file():
        return Response.error(404, "Not found")

    modified = int(candidate.stat().st_mtime)
    since = request.headers.get("If-Modified-Since")
    if since:
        try:
            if modified <= int(parsedate_to_datetime(since).timestamp()):
                return Response(304, b"", {"Last-Modified": formatdate(modified, usegmt=True)})
        except (TypeError, ValueError):
            pass  # an unparseable date is ignored, as RFC 9110 requires
    content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
        content_type += "; charset=utf-8"
    return Response(200, candidate.read_bytes(), {
        "Content-Type": content_type,
        "Last-Modified": formatdate(modified, usegmt=True),
    })
