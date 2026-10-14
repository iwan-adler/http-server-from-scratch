import json

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from minihttp.app import App
from minihttp.chat import ChatRooms
from minihttp.server import Server


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def chat():
    clock = Clock()
    rooms = ChatRooms(history=3, rate_limit=3, rate_window=10, clock=clock)
    app = App()
    app.websocket("/ws/chat")(rooms.handle)
    srv = Server(app, port=0, workers=32)
    port = srv.start()
    yield f"ws://127.0.0.1:{port}/ws/chat", clock
    srv.shutdown()


def join(base: str, room: str, name: str):
    client = connect(f"{base}?room={room}&name={name}")
    history = json.loads(client.recv())
    assert history["type"] == "history"
    return client, history


def next_event(client, kind: str) -> dict:
    while True:
        event = json.loads(client.recv(timeout=3))
        if event["type"] == kind:
            return event


def test_messages_reach_everyone_in_the_room_only(chat):
    base, _ = chat
    ada, _ = join(base, "dsa", "Ada")
    grace, _ = join(base, "dsa", "Grace")
    other, _ = join(base, "ml", "Linus")
    assert next_event(ada, "join")["users"] == 1   # her own join
    assert next_event(ada, "join") == {"type": "join", "name": "Grace", "users": 2}

    grace.send(json.dumps({"text": "  hello dsa  "}))
    for client in (ada, grace):
        message = next_event(client, "message")
        assert message["name"] == "Grace" and message["text"] == "hello dsa"  # trimmed
    with pytest.raises(TimeoutError):
        next_event(other, "message")  # a different room hears nothing
    for c in (ada, grace, other):
        c.close()


def test_history_is_sent_on_join_and_capped(chat):
    base, clock = chat
    ada, _ = join(base, "room1", "Ada")
    for i in range(5):
        clock.now += 11  # stay under the fixture's rate limit (3 per 10 s)
        ada.send(json.dumps({"text": f"m{i}"}))
        next_event(ada, "message")
    late, history = join(base, "room1", "Late")
    assert [m["text"] for m in history["messages"]] == ["m2", "m3", "m4"]  # history=3
    ada.close()
    late.close()


def test_leave_is_announced(chat):
    base, _ = chat
    ada, _ = join(base, "r", "Ada")
    grace, _ = join(base, "r", "Grace")
    grace.close()
    assert next_event(ada, "leave") == {"type": "leave", "name": "Grace", "users": 1}
    ada.close()


@pytest.mark.parametrize(("payload", "error"), [
    ("not json", "must be JSON"),
    (json.dumps({"text": ""}), "non-empty"),
    (json.dumps({"text": 5}), "non-empty"),
    (json.dumps(["text"]), "non-empty"),
    (json.dumps({"text": "x" * 501}), "at most 500"),
    (b"binary", "binary"),
])
def test_invalid_messages_get_an_error_not_a_disconnect(chat, payload, error):
    base, _ = chat
    client, _ = join(base, "r", "Ada")
    client.send(payload)
    assert error in next_event(client, "error")["error"]
    client.send(json.dumps({"text": "still connected"}))
    assert next_event(client, "message")["text"] == "still connected"
    client.close()


def test_rate_limit(chat):
    base, clock = chat
    client, _ = join(base, "r", "Ada")
    for i in range(3):
        client.send(json.dumps({"text": f"m{i}"}))
        next_event(client, "message")
    client.send(json.dumps({"text": "too fast"}))
    assert "slow down" in next_event(client, "error")["error"]
    clock.now += 11  # the window has passed
    client.send(json.dumps({"text": "ok again"}))
    assert next_event(client, "message")["text"] == "ok again"
    client.close()


@pytest.mark.parametrize("query", ["room=dsa&name=", "room=dsa&name=" + "x" * 25, "room=Bad%20Room&name=Ada",
                                   "room=dsa&name=<script>"])
def test_invalid_room_or_name_is_refused_with_1008(chat, query):
    base, _ = chat
    with connect(f"{base}?{query}") as client:
        with pytest.raises(ConnectionClosed) as excinfo:
            client.recv(timeout=3)
    assert excinfo.value.rcvd.code == 1008
