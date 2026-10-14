"""Multi-room chat over WebSockets.

Protocol (JSON text messages):
  client -> server  {"text": "hello"}
  server -> client  {"type": "history", "messages": [...]}       sent once, on join
                    {"type": "message", "name", "text", "at"}
                    {"type": "join" | "leave", "name", "users"}
                    {"type": "error", "error": "..."}
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone

from minihttp import websocket
from minihttp.request import Request
from minihttp.websocket import WebSocket, WebSocketClosed

NAME = re.compile(r"^[\w .'-]{1,24}$")   # letters/digits in any language, plus a few separators
ROOM = re.compile(r"^[a-z0-9-]{1,32}$")
MAX_TEXT = 500


class ChatRooms:
    def __init__(self, history: int = 50, rate_limit: int = 10, rate_window: float = 10.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._rooms: dict[str, set[WebSocket]] = {}
        self._history: dict[str, deque] = {}
        self._history_size = history
        self._lock = threading.Lock()
        self.rate_limit, self.rate_window, self._clock = rate_limit, rate_window, clock

    def users(self, room: str) -> int:
        with self._lock:
            return len(self._rooms.get(room, ()))

    def broadcast(self, room: str, event: dict) -> None:
        payload = json.dumps(event, ensure_ascii=False)
        with self._lock:
            members = list(self._rooms.get(room, ()))
        for member in members:
            try:
                member.send_text(payload)
            except WebSocketClosed:
                self._leave(room, member)

    def _leave(self, room: str, ws: WebSocket) -> bool:
        with self._lock:
            members = self._rooms.get(room, set())
            if ws not in members:
                return False
            members.discard(ws)
            if not members:
                self._rooms.pop(room, None)
            return True

    def handle(self, ws: WebSocket, request: Request) -> None:
        """The WebSocket handler for /ws/chat?room=<room>&name=<name>."""
        room = request.query.get("room", ["general"])[0].strip().lower()
        name = request.query.get("name", [""])[0].strip()
        if not ROOM.match(room) or not NAME.match(name):
            ws.close(websocket.POLICY_VIOLATION, "invalid room or name")
            return

        with self._lock:
            self._rooms.setdefault(room, set()).add(ws)
            history = list(self._history.setdefault(room, deque(maxlen=self._history_size)))
        ws.send_text(json.dumps({"type": "history", "room": room, "messages": history}, ensure_ascii=False))
        self.broadcast(room, {"type": "join", "name": name, "users": self.users(room)})

        sent: deque[float] = deque()
        try:
            while (raw := ws.recv()) is not None:
                error, text = self._accept(raw, sent)
                if error:
                    ws.send_text(json.dumps({"type": "error", "error": error}))
                    continue
                event = {"type": "message", "name": name, "text": text,
                         "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                with self._lock:
                    self._history[room].append(event)
                self.broadcast(room, event)
        finally:
            if self._leave(room, ws):
                self.broadcast(room, {"type": "leave", "name": name, "users": self.users(room)})

    def _accept(self, raw: str | bytes, sent: deque) -> tuple[str | None, str | None]:
        """Validate one incoming message. Returns (error, None) or (None, text)."""
        if isinstance(raw, bytes):
            return "binary messages are not supported", None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return 'messages must be JSON like {"text": "..."}', None
        text = data.get("text") if isinstance(data, dict) else None
        if not isinstance(text, str) or not text.strip():
            return "text must be a non-empty string", None
        if len(text) > MAX_TEXT:
            return f"text must be at most {MAX_TEXT} characters", None
        now = self._clock()
        while sent and now - sent[0] > self.rate_window:
            sent.popleft()
        if len(sent) >= self.rate_limit:
            return f"slow down: at most {self.rate_limit} messages per {self.rate_window:g} s", None
        sent.append(now)
        return None, text.strip()
