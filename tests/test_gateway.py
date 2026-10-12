"""Gateway tests: handshake, dispatch, close-code policy, reconnect, shutdown.

The fake socket blocks in ``recv_json`` when it has nothing to deliver,
exactly like a live one, so a reader task left behind by a bad teardown
shows up here as a second consumer of the same frames.
"""

import asyncio
import json

import pytest

from despise import gateway as G
from despise.exceptions import (
    AuthenticationFailed,
    GatewayClosed,
    GatewayTimeout,
    NotConnected,
    TooManySubscriptions,
)
from despise.gateway import (
    DISPATCH,
    HEARTBEAT,
    HEARTBEAT_ACK,
    HELLO,
    IDENTIFY,
    INVALID_SESSION,
    PRESENCE_UPDATE,
    QOS,
    RECONNECT,
    RESUME,
    Gateway,
)
from despise.headers import EphemeralIds, build_profile
from despise.http import DespiseHttp
from despise.models import User

EPH = EphemeralIds(client_launch_id="launch-1")


def hello(interval_ms=41250):
    return {"op": HELLO, "d": {"heartbeat_interval": interval_ms}}


def ready(seq=1):
    return {
        "op": DISPATCH,
        "s": seq,
        "t": "READY",
        "d": {
            "session_id": "sess-1",
            "resume_gateway_url": "wss://resume.discord.gg",
            "user": {"id": "9", "username": "t"},
        },
    }


def resumed(seq=9):
    return {"op": DISPATCH, "s": seq, "t": "RESUMED", "d": {}}


def create(seq=2, t="MESSAGE_CREATE", d=None):
    return {"op": DISPATCH, "s": seq, "t": t, "d": d if d is not None else {"id": "m1"}}


class FakeWS:
    """Live-socket stand-in: queues inbound frames, records outbound ones."""

    def __init__(self, inbound=()):
        self.inbound = list(inbound)
        self.sent = []
        self.sent_bytes = []
        self.close_code = None
        self.close_reason = None
        self.closed = False
        self._new = asyncio.Event()

    async def send_json(self, payload):
        self.sent.append(payload)

    async def send_bytes(self, payload):
        self.sent_bytes.append(payload)

    async def recv_json(self):
        frame = await self._next()
        if isinstance(frame, bytes):
            import json as _json

            return _json.loads(frame)
        return frame

    async def recv(self):
        """Raw frame access, as zlib-stream requires."""
        frame = await self._next()
        if isinstance(frame, bytes):
            return frame, 0b0010
        return json.dumps(frame).encode(), 0b0001

    async def _next(self):
        while not self.inbound:
            self._new.clear()
            await self._new.wait()
        item = self.inbound.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def feed(self, *frames):
        self.inbound.extend(frames)
        self._new.set()

    def drop(self, code=None, reason=None, exc=None):
        """Simulate a server-side close carrying a WebSocket close code."""
        exc = exc or ConnectionResetError("socket died")
        self.close_code = code
        self.close_reason = reason
        self.inbound.append(exc)
        self._new.set()


class FakeCtx:
    def __init__(self, ws):
        self.ws = ws

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, *a):
        self.ws.closed = True
        return False


class FakeSession:
    """Hands out one prepared socket per ws_connect, in order."""

    def __init__(self, sockets):
        self.sockets = list(sockets)
        self.urls = []

    def ws_connect(self, url):
        self.urls.append(url)
        if not self.sockets:
            raise AssertionError(f"unexpected extra ws_connect: {url}")
        return FakeCtx(self.sockets.pop(0))

    async def close(self):
        pass


class FakeHttp(DespiseHttp):
    def __init__(self, sockets):
        profile = build_profile("web")
        super().__init__(profile, EPH)
        self.session = FakeSession(sockets)  # type: ignore[assignment]

    async def request(self, method, path, **kwargs):
        assert path == "/gateway"
        return {"url": "wss://gateway.discord.gg"}


def make_gateway(*sockets, encoding="json", session_cache=None):
    """A gateway with zero backoff so reconnects are instantaneous."""
    http = FakeHttp(sockets)
    gw = Gateway(
        http,
        "tok",
        max_backoff=0.0,
        encoding=encoding,
        session_cache=session_cache,
    )
    gw._classify = _no_backoff(gw)
    return gw


def _no_backoff(gw):
    original = gw._classify

    def classify(code, reason, resume_hint):
        resume, _delay, fatal = original(code, reason, resume_hint)
        return resume, 0.0, fatal

    return classify


def run(coro):
    return asyncio.run(coro)


async def wait_until(pred, timeout=2.0):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.005)


# -- handshake --------------------------------------------------------------
def test_identify_and_ready():
    ws = FakeWS([hello(), ready()])
    gw = make_gateway(ws)

    async def main():
        await gw.connect()
        assert gw.is_ready
        assert gw.session_id == "sess-1"
        assert gw.resume_url == "wss://resume.discord.gg"
        assert isinstance(gw.user, User) and gw.user.id == "9"
        await gw.close()

    run(main())
    ops = [p["op"] for p in ws.sent]
    assert ops[0] == HEARTBEAT  # immediate heartbeat must precede anything else
    assert ops[1] == IDENTIFY
    ident = ws.sent[1]["d"]
    assert ident["token"] == "tok"
    assert ident["capabilities"] == 1734653
    assert ident["client_state"] == {"guild_versions": {}, "api_code_version": 0}
    assert "is_fast_connect" in ident["properties"]
    assert ops[2] == QOS and ws.sent[2]["d"]["version"] == 29
    assert gw.session_id is None  # close() drops the session


def test_ws_url_carries_version_and_encoding():
    ws = FakeWS([hello(), ready()])
    gw = make_gateway(ws)

    async def main():
        await gw.connect()
        await gw.close()

    run(main())
    assert gw.http.session.urls == ["wss://gateway.discord.gg?v=9&encoding=json"]


def test_ready_timeout_tears_down_and_raises(monkeypatch):
    monkeypatch.setattr(G, "READY_TIMEOUT_S", 0.05)
    ws = FakeWS([hello()])  # never sends READY
    gw = make_gateway(ws)

    with pytest.raises(GatewayTimeout):
        run(gw.connect())
    assert ws.closed is True
    assert gw.is_ready is False


def test_hello_expected_not_asserted():
    """Protocol validation must survive `python -O`."""
    ws = FakeWS([{"op": DISPATCH, "s": 1, "t": "READY", "d": {}}])
    gw = make_gateway(ws)
    with pytest.raises(GatewayClosed, match="expected HELLO"):
        run(gw.connect())


# -- dispatch ---------------------------------------------------------------
def test_dispatch_emits_event():
    ws = FakeWS([hello(), ready(), create()])
    gw = make_gateway(ws)
    got = []

    async def main():
        await gw.connect()
        await wait_until(lambda: got)
        assert gw.seq == 2
        await gw.close()

    gw.on("MESSAGE_CREATE", got.append)
    run(main())
    assert got == [{"id": "m1"}]


def test_listener_exception_does_not_kill_the_loop():
    ws = FakeWS([hello(), ready(), create(seq=2), create(seq=3)])
    gw = make_gateway(ws)
    seen, errors = [], []

    async def boom(_):
        raise ValueError("listener blew up")

    async def main():
        await gw.connect()
        # the reader survived both failures and kept consuming
        await wait_until(lambda: gw.seq == 3)
        await gw.close()

    gw.on("MESSAGE_CREATE", boom)
    gw.on("MESSAGE_CREATE", seen.append)
    gw.on_error(lambda event, exc: errors.append((event, exc)))
    run(main())
    assert len(seen) == 2, "second listener still received both frames"
    assert len(errors) == 2, "each failure was reported, none escaped"
    assert errors[0][0] == "MESSAGE_CREATE"
    assert isinstance(errors[0][1], ValueError)


def test_sync_listener_supported():
    ws = FakeWS([hello(), ready(), create()])
    gw = make_gateway(ws)
    got = []

    async def main():
        await gw.connect()
        await wait_until(lambda: got)
        await gw.close()

    gw.on("MESSAGE_CREATE", got.append)  # not a coroutine function
    run(main())
    assert got == [{"id": "m1"}]


def test_wait_for_with_check():
    ws = FakeWS([hello(), ready()])
    gw = make_gateway(ws)

    async def main():
        await gw.connect()
        waiter = asyncio.create_task(
            gw.wait_for("MESSAGE_CREATE", check=lambda d: d["id"] == "m2")
        )
        await asyncio.sleep(0)  # let the waiter register before anything arrives
        ws.feed(create(d={"id": "m1"}), create(d={"id": "m2"}))
        result = await asyncio.wait_for(waiter, 2.0)
        await gw.close()
        return result

    assert run(main())["id"] == "m2"


def test_wait_for_timeout_cleans_up():
    ws = FakeWS([hello(), ready()])
    gw = make_gateway(ws)

    async def main():
        await gw.connect()
        with pytest.raises(asyncio.TimeoutError):
            await gw.wait_for("NEVER_HAPPENS", timeout=0.05)
        assert not gw._waiters.get("NEVER_HAPPENS")
        await gw.close()

    run(main())


# -- close-code policy ------------------------------------------------------
def test_fatal_close_code_stops_and_raises():
    ws = FakeWS([hello(), ready()])
    gw = make_gateway(ws)

    async def main():
        await gw.connect()
        ws.drop(code=4004, reason="Authentication failed.")
        await gw.wait_closed()

    with pytest.raises(AuthenticationFailed):
        run(main())
    assert gw.closed is True


def test_fatal_intents_code_raises_invalid_intents():
    ws = FakeWS([hello(), ready()])
    gw = make_gateway(ws)

    async def main():
        await gw.connect()
        ws.drop(code=4014, reason="Invalid intents")
        await gw.wait_closed()

    with pytest.raises(GatewayClosed, match="4014"):
        run(main())


def test_fresh_close_code_reidentifies():
    ws1, ws2 = FakeWS([hello(), ready(seq=5)]), FakeWS([hello(), ready(seq=1)])
    gw = make_gateway(ws1, ws2)

    async def main():
        await gw.connect()
        ws1.drop(code=4007, reason="Invalid seq")
        await wait_until(lambda: len(ws2.sent) > 1)
        await gw.close()

    run(main())
    assert ws2.sent[1]["op"] == IDENTIFY  # 4007 must re-identify, not resume
    assert gw.http.session.urls[1] == "wss://gateway.discord.gg?v=9&encoding=json"


def test_normal_disconnect_resumes():
    ws1, ws2 = FakeWS([hello(), ready(seq=5)]), FakeWS([hello(), resumed()])
    gw = make_gateway(ws1, ws2)

    async def main():
        await gw.connect()
        ws1.drop(code=1006)  # abnormal, no clean close frame
        await wait_until(lambda: len(ws2.sent) > 1)
        await gw.close()

    run(main())
    assert ws2.sent[1]["op"] == RESUME
    assert ws2.sent[1]["d"] == {"token": "tok", "session_id": "sess-1", "seq": 5}
    assert gw.http.session.urls[1] == "wss://resume.discord.gg?v=9&encoding=json"


def test_invalid_session_resumable_reconnects():
    ws1 = FakeWS([hello(), ready()])
    ws2 = FakeWS([hello(), {"op": INVALID_SESSION, "d": True}, hello(), resumed()])
    gw = make_gateway(ws1, ws2)

    async def main():
        await gw.connect()
        ws1.feed({"op": INVALID_SESSION, "d": True})
        await wait_until(lambda: len(ws2.sent) > 1)
        await gw.close()

    run(main())
    assert ws2.sent[1]["op"] == RESUME


def test_invalid_session_non_resumable_reidentifies():
    ws1 = FakeWS([hello(), ready()])
    ws2 = FakeWS([hello(), {"op": INVALID_SESSION, "d": False}, hello(), ready()])
    gw = make_gateway(ws1, ws2)

    async def main():
        await gw.connect()
        ws1.feed({"op": INVALID_SESSION, "d": False})
        await wait_until(lambda: len(ws2.sent) > 1)
        await gw.close()

    run(main())
    assert ws2.sent[1]["op"] == IDENTIFY


def test_reconnect_op_resumes():
    ws1 = FakeWS([hello(), ready()])
    ws2 = FakeWS([hello(), {"op": RECONNECT, "d": None}, hello(), resumed()])
    gw = make_gateway(ws1, ws2)

    async def main():
        await gw.connect()
        ws1.feed({"op": RECONNECT, "d": None})
        await wait_until(lambda: len(ws2.sent) > 1)
        await gw.close()

    run(main())
    assert ws2.sent[1]["op"] == RESUME


def test_server_heartbeat_request_is_answered():
    ws = FakeWS([hello(), ready()])
    gw = make_gateway(ws)

    async def main():
        await gw.connect()
        before = len(ws.sent)
        ws.feed({"op": 1, "d": None})  # server asks for an immediate heartbeat
        await wait_until(lambda: len(ws.sent) > before)
        await gw.close()

    run(main())
    assert ws.sent[-1] == {"op": HEARTBEAT, "d": 1}  # echoes the last seq


def test_heartbeat_ack_clears_zombie_state():
    ws = FakeWS([hello(interval_ms=10_000), ready()])
    gw = make_gateway(ws)

    async def main():
        await gw.connect()
        gw._acked.clear()
        assert not gw._acked.is_set()
        ws.feed({"op": HEARTBEAT_ACK, "d": None})
        await wait_until(lambda: gw._acked.is_set())
        await gw.close()

    run(main())


def test_no_leaked_reader_after_reconnect():
    """The old reader must be gone: only one task may read a given socket."""
    ws1 = FakeWS([hello(), ready(seq=3)])
    ws2 = FakeWS([hello(), resumed(seq=4), create(seq=5)])
    gw = make_gateway(ws1, ws2)
    got = []

    async def main():
        await gw.connect()
        first_reader = gw._reader_task
        ws1.drop(code=1006)
        await wait_until(lambda: len(ws2.sent) > 1)
        assert gw._reader_task is not first_reader
        # the old reader owns the dead socket's cleanup and exits right after
        await wait_until(first_reader.done)
        await wait_until(lambda: got)
        await gw.close()

    gw.on("MESSAGE_CREATE", got.append)
    run(main())
    assert got == [{"id": "m1"}]  # consumed exactly once, by the new reader


def test_heartbeat_zombie_actually_reconnects():
    """Regression: the beat loop used to cancel itself inside _reconnect()."""
    ws1 = FakeWS([hello(interval_ms=10), ready()])
    ws2 = FakeWS([hello(interval_ms=10), resumed()])
    gw = make_gateway(ws1, ws2)

    async def main():
        await gw.connect()
        gw._acked.clear()  # server never acks
        await wait_until(lambda: len(ws2.sent) > 1)
        assert ws2.sent[1]["op"] == RESUME
        await gw.close()

    run(main())


def test_ack_rearmed_on_reconnect():
    """A beat cancelled between clear() and ack must not look like a zombie."""
    ws1 = FakeWS([hello(interval_ms=10_000), ready()])
    ws2 = FakeWS([hello(interval_ms=10_000), resumed()])
    gw = make_gateway(ws1, ws2)

    async def main():
        await gw.connect()
        gw._acked.clear()
        ws1.drop(code=1006)
        await wait_until(lambda: len(ws2.sent) > 1)
        assert gw._acked.is_set()
        await gw.close()

    run(main())


# -- socket ops -------------------------------------------------------------
def test_presence_throttle_is_per_gateway():
    """Regression: one module-global timestamp throttled every client at once."""
    ws_a = FakeWS([hello(), ready()])
    ws_b = FakeWS([hello(), ready()])
    a, b = make_gateway(ws_a), make_gateway(ws_b)

    async def main():
        await a.connect()
        await b.connect()
        # a is throttled by its own previous send
        await a.send_presence([{"name": "A", "type": 0}], force=True)
        loop = asyncio.get_event_loop()
        start = loop.time()
        await a.send_presence([{"name": "A2", "type": 0}])
        a_elapsed = loop.time() - start
        # b never sent, so b's own clock is fresh and it must not wait
        start = loop.time()
        await b.send_presence([{"name": "B", "type": 0}])
        b_elapsed = loop.time() - start
        await a.close()
        await b.close()
        return a_elapsed, b_elapsed

    a_elapsed, b_elapsed = run(main())
    assert a_elapsed >= 4.5, "a's own throttle should still apply"
    assert b_elapsed < 0.5, "b must not inherit a's throttle window"


def test_presence_sent_when_connected():
    ws = FakeWS([hello(), ready()])
    gw = make_gateway(ws)

    async def main():
        await gw.connect()
        await gw.send_presence([{"name": "Chess", "type": 5}], force=True)
        await gw.close()

    run(main())
    op3 = [p for p in ws.sent if p["op"] == PRESENCE_UPDATE]
    assert op3 and op3[0]["d"]["activities"] == [{"name": "Chess", "type": 5}]


def test_presence_when_disconnected_raises():
    gw = make_gateway(FakeWS([hello(), ready()]))
    with pytest.raises(NotConnected):
        run(gw.send_presence([{"name": "x", "type": 0}]))


def test_subscribe_guild_uses_public_send():
    ws = FakeWS([hello(), ready()])
    gw = make_gateway(ws)

    async def main():
        await gw.connect()
        await gw.subscribe_guild("g1")
        nonce = await gw.request_members("g1", query="bob")
        await gw.close()
        return nonce

    nonce = run(main())
    assert ws.sent[-2]["op"] == 14
    assert ws.sent[-2]["d"]["guild_id"] == "g1"
    assert ws.sent[-1]["op"] == 8
    assert ws.sent[-1]["d"]["query"] == "bob"
    assert ws.sent[-1]["d"]["nonce"] == nonce


# -- presence on re-identify ------------------------------------------------
def test_first_identify_carries_no_presence():
    ws = FakeWS([hello(), ready()])
    gw = make_gateway(ws)

    async def main():
        await gw.connect()
        await gw.close()

    run(main())
    assert "presence" not in ws.sent[1]["d"]


def test_identify_carries_no_intents():
    """User accounts get every dispatch without intents; sending the field
    is bot-like behavior that flags the account."""
    ws = FakeWS([hello(), ready()])
    gw = make_gateway(ws)

    async def main():
        await gw.connect()
        await gw.close()

    run(main())
    assert "intents" not in ws.sent[1]["d"]


def test_reidentify_replays_the_last_presence():
    """Regression: a fresh re-identify silently reset the account's activity."""
    ws1 = FakeWS([hello(), ready()])
    ws2 = FakeWS([hello(), ready()])
    gw = make_gateway(ws1, ws2)

    async def main():
        await gw.connect()
        await gw.send_presence(
            [{"name": "Chess", "type": 5}], status="dnd", force=True
        )
        # non-resumable invalid session -> must re-Identify, not Resume
        ws1.feed({"op": INVALID_SESSION, "d": False})
        await wait_until(lambda: len(ws2.sent) > 1)
        await gw.close()

    run(main())
    assert ws2.sent[1]["op"] == IDENTIFY
    presence = ws2.sent[1]["d"]["presence"]
    assert presence["status"] == "dnd"
    assert presence["activities"] == [{"name": "Chess", "type": 5}]


# -- max_concurrency --------------------------------------------------------
def ready_with(concurrency=None, seq=1):
    payload = {
        "session_id": "sess-1",
        "resume_gateway_url": "wss://resume.discord.gg",
        "user": {"id": "9", "username": "t"},
    }
    if concurrency is not None:
        payload["max_concurrency"] = concurrency
    return {"op": DISPATCH, "s": seq, "t": "READY", "d": payload}


def test_max_concurrency_read_from_ready():
    ws = FakeWS([hello(), ready_with(concurrency=2)])
    gw = make_gateway(ws)

    async def main():
        await gw.connect()
        assert gw.max_concurrency == 2
        await gw.close()

    run(main())


def test_subscribe_beyond_max_concurrency_raises():
    """Exceeding it server-side is close 4013, which costs a reconnect."""
    ws = FakeWS([hello(), ready_with(concurrency=2)])
    gw = make_gateway(ws)

    async def main():
        await gw.connect()
        await gw.subscribe_guild("g1")
        await gw.subscribe_guild("g2")
        with pytest.raises(TooManySubscriptions) as excinfo:
            await gw.subscribe_guild("g3")
        assert excinfo.value.limit == 2
        assert excinfo.value.guild_id == "g3"
        # re-subscribing an existing guild is not a new slot
        await gw.subscribe_guild("g1")
        await gw.unsubscribe_guild("g1")
        await gw.subscribe_guild("g3")
        await gw.close()

    run(main())


def test_unsubscribe_frees_a_slot():
    ws = FakeWS([hello(), ready_with(concurrency=1)])
    gw = make_gateway(ws)

    async def main():
        await gw.connect()
        await gw.subscribe_guild("only")
        with pytest.raises(TooManySubscriptions):
            await gw.subscribe_guild("other")
        await gw.unsubscribe_guild("only")
        await gw.subscribe_guild("other")
        assert gw.subscribed_guilds == {"other"}
        await gw.close()

    run(main())
    off = [p for p in ws.sent if p["op"] == 14 and not p["d"]["typing"]]
    assert off and off[0]["d"]["guild_id"] == "only"


def test_guild_delete_frees_a_slot():
    ws = FakeWS([hello(), ready_with(concurrency=1)])
    gw = make_gateway(ws)
    seen = []

    async def main():
        await gw.connect()
        await gw.subscribe_guild("g1")
        ws.feed({"op": DISPATCH, "s": 2, "t": "GUILD_DELETE", "d": {"guild_id": "g1"}})
        await wait_until(lambda: seen)
        assert gw.subscribed_guilds == set()
        await gw.close()

    gw.on("GUILD_DELETE", seen.append)
    run(main())


def test_subscribe_without_max_concurrency_is_unbounded():
    ws = FakeWS([hello(), ready()])  # no max_concurrency in payload
    gw = make_gateway(ws)

    async def main():
        await gw.connect()
        assert gw.max_concurrency is None
        for i in range(5):
            await gw.subscribe_guild(f"g{i}")
        assert len(gw.subscribed_guilds) == 5
        await gw.close()

    run(main())


# -- lazy member list sync --------------------------------------------------
def test_lazy_load_members_opens_a_sync():
    ws = FakeWS([hello(), ready()])
    gw = make_gateway(ws)

    async def main():
        await gw.connect()
        nonce = await gw.lazy_load_members("g1")
        await gw.close()
        return nonce

    nonce = run(main())
    req = ws.sent[-1]
    assert req["op"] == 8
    assert req["d"]["limit"] == 0  # 0 is what opts into lazy loading
    assert req["d"]["nonce"] == nonce


def test_member_sync_chunks_are_dispatched():
    ws = FakeWS([hello(), ready()])
    gw = make_gateway(ws)
    generic, specific = [], []
    chunk = {
        "op": 12,
        "t": "GUILD_MEMBERS_UPDATE",
        "d": {
            "guild_id": "g1",
            "members": [{"user": {"id": "1"}}],
            "chunk_index": 0,
            "chunk_count": 2,
            "nonce": "n1",
        },
    }

    async def main():
        await gw.connect()
        ws.feed(chunk)
        await wait_until(lambda: generic)
        await gw.close()

    gw.on("GUILD_MEMBERS_UPDATE", generic.append)
    gw.on("SYNC_MEMBERS", specific.append)
    run(main())
    assert generic[0]["chunk_count"] == 2
    assert specific == []  # only the frame's own name fires, not SYNC_MEMBERS


def test_lazy_load_failure_is_dispatched():
    ws = FakeWS([hello(), ready()])
    gw = make_gateway(ws)
    seen = []

    async def main():
        await gw.connect()
        ws.feed(
            {
                "op": 12,
                "t": "LAZY_LOAD_FAILED",
                "d": {"guild_id": "g1", "nonce": "n1", "members_missing": True},
            }
        )
        await wait_until(lambda: seen)
        await gw.close()

    gw.on("LAZY_LOAD_FAILED", seen.append)
    run(main())
    assert seen[0]["members_missing"] is True


# -- zlib-stream framing ----------------------------------------------------
def zframe(obj) -> bytes:
    """Under encoding=zlib-stream the server compresses *every* frame, HELLO
    included -- so the fixtures must be pre-compressed."""
    import zlib

    return zlib.compress(json.dumps(obj).encode())


def test_ws_url_reflects_the_encoding():
    ws = FakeWS([hello(), ready()])
    gw = make_gateway(ws)

    async def main():
        await gw.connect()
        await gw.close()

    run(main())
    assert gw.http.session.urls == ["wss://gateway.discord.gg?v=9&encoding=json"]


def test_zlib_stream_url():
    ws = FakeWS([zframe(hello()), zframe(ready())])
    gw = make_gateway(ws, encoding="zlib-stream")

    async def main():
        await gw.connect()
        await gw.close()

    run(main())
    assert gw.http.session.urls == [
        "wss://gateway.discord.gg?v=9&encoding=zlib-stream"
    ]


def test_zlib_stream_roundtrip():
    import zlib

    ws = FakeWS([zframe(hello()), zframe(ready())])
    gw = make_gateway(ws, encoding="zlib-stream")
    got = []

    async def main():
        await gw.connect()
        await gw.subscribe_guild("g1")
        assert gw.subscribed_guilds == {"g1"}
        ws.feed(zframe(create(d={"id": "z1"})))
        await wait_until(lambda: got)
        await gw.close()

    gw.on("MESSAGE_CREATE", got.append)
    run(main())
    assert ws.sent == []  # nothing went out as plain JSON
    assert ws.sent_bytes, "expected zlib-compressed frames"
    decoded = json.loads(zlib.decompress(ws.sent_bytes[1]))
    assert decoded["op"] == IDENTIFY
    assert decoded["d"]["token"] == "tok"
    assert json.loads(zlib.decompress(ws.sent_bytes[-1]))["op"] == 14


def test_unknown_encoding_rejected():
    with pytest.raises(ValueError, match="unknown gateway encoding"):
        make_gateway(FakeWS(), encoding="brotli")


# -- session cache integration ---------------------------------------------
class SpyCache:
    """Minimal SessionCache stand-in; records every call."""

    def __init__(self, state=None):
        self.state = state
        self.saves = []
        self.clears = 0
        self.loads = 0

    async def load(self):
        self.loads += 1
        return self.state

    async def save(self, session_id, seq=None, resume_url=None, *, force=False):
        self.saves.append((session_id, seq, resume_url, force))
        return True

    async def clear(self):
        self.clears += 1
        self.state = None

    async def close(self):
        pass


def test_ready_persists_the_session():
    cache = SpyCache()
    ws = FakeWS([hello(), ready(seq=7)])
    gw = make_gateway(ws, session_cache=cache)

    async def main():
        await gw.connect()
        await gw.close()

    run(main())
    assert cache.saves[0] == ("sess-1", 7, "wss://resume.discord.gg", True)


def test_cached_session_is_adopted_and_resumed():
    """A restored session turns the next connect into a Resume, not Identify."""
    cache = SpyCache(SessionStateShim("sess-cached", 99, "wss://cached.gg"))
    ws = FakeWS([hello(), resumed(seq=100)])
    gw = make_gateway(ws, session_cache=cache)

    async def main():
        await gw.connect()
        await gw.close()

    run(main())
    assert cache.loads == 1
    assert ws.sent[1]["op"] == RESUME
    assert ws.sent[1]["d"]["session_id"] == "sess-cached"
    assert gw.http.session.urls == ["wss://cached.gg?v=9&encoding=json"]


def SessionStateShim(session_id, seq, resume_url):
    from despise.session_cache import SessionState

    return SessionState(session_id=session_id, seq=seq, resume_url=resume_url)


def test_close_discards_the_cached_session():
    """A deliberate shutdown must not leave a resumable session behind."""
    cache = SpyCache()
    ws = FakeWS([hello(), ready()])
    gw = make_gateway(ws, session_cache=cache)

    async def main():
        await gw.connect()
        await gw.close()

    run(main())
    assert cache.clears >= 1


def test_fresh_close_code_forgets_the_session():
    cache = SpyCache()
    ws1 = FakeWS([hello(), ready(seq=5)])
    ws2 = FakeWS([hello(), ready(seq=1)])
    gw = make_gateway(ws1, ws2, session_cache=cache)

    async def main():
        await gw.connect()
        ws1.drop(code=4007, reason="Invalid seq")
        await wait_until(lambda: len(ws2.sent) > 1)
        await gw.close()

    run(main())
    assert cache.clears >= 1
    assert ws2.sent[1]["op"] == IDENTIFY


def test_fatal_close_forgets_the_session():
    cache = SpyCache()
    ws = FakeWS([hello(), ready()])
    gw = make_gateway(ws, session_cache=cache)

    async def main():
        await gw.connect()
        ws.drop(code=4004, reason="Authentication failed.")
        await gw.wait_closed()

    with pytest.raises(AuthenticationFailed):
        run(main())
    assert cache.clears >= 1


def test_seq_is_persisted_throttled_between_dispatches():
    """READY forces a write; per-dispatch seq updates go through the throttle."""
    cache = SpyCache()
    ws = FakeWS([hello(), ready(seq=1)])
    gw = make_gateway(ws, session_cache=cache)

    async def main():
        await gw.connect()
        ws.feed(create(seq=2), create(seq=3), create(seq=4))
        await wait_until(lambda: gw.seq == 4)
        await gw.close()

    run(main())
    # first save is READY's forced write; the rest go to the real throttle
    assert cache.saves[0][3] is True
    assert all(not s[3] for s in cache.saves[1:])


def test_no_cache_means_no_cache_calls():
    ws = FakeWS([hello(), ready()])
    gw = make_gateway(ws)  # session_cache=None

    async def main():
        await gw.connect()
        await gw.close()

    run(main())
    assert gw.session_cache is None


# -- shutdown ---------------------------------------------------------------
def test_close_is_final():
    ws = FakeWS([hello(), ready()])
    gw = make_gateway(ws)

    async def main():
        await gw.connect()
        await gw.close()
        assert gw.closed
        assert gw.session_id is None
        # a late drop must not resurrect anything
        ws.drop(code=1006)
        await asyncio.sleep(0.05)
        assert gw.closed
        assert len(gw.http.session.urls) == 1
        await gw.wait_closed()  # returns quietly, no fatal error

    run(main())


def test_close_without_connect_is_safe():
    gw = make_gateway(FakeWS())
    run(gw.close())
    assert gw.closed is True


# -- reconnect retries ------------------------------------------------------
def dead_socket():
    """A socket that dies before it can even send HELLO."""
    return FakeWS([ConnectionResetError("refused")])


def test_transient_handshake_failure_retries(monkeypatch):
    ws1 = FakeWS([hello(), ready(seq=4)])
    gw = make_gateway(ws1)
    gw.http.session.sockets.append(dead_socket())
    gw.http.session.sockets.append(FakeWS([hello(), resumed(seq=5)]))
    resumed_seen = []

    async def main():
        await gw.connect()
        ws1.drop(code=1006)
        await wait_until(lambda: resumed_seen)
        await gw.close()

    gw.on("RESUMED", resumed_seen.append)
    run(main())
    assert gw.http.session.sockets == []  # both retries consumed


def test_close_during_handshake_leaves_nothing_running():
    """Regression: close() used to orphan the socket and its tasks."""
    ws1 = FakeWS([hello(), ready()])
    gw = make_gateway(ws1)
    # never sends READY, so the handshake is still in flight when we close
    gw.http.session.sockets.append(FakeWS([hello()]))

    async def main():
        await gw.connect()
        drop = asyncio.create_task(gw._reconnect(resume=True, cause="test"))
        await wait_until(lambda: len(gw.http.session.urls) >= 2)
        await gw.close()
        await asyncio.sleep(0.1)
        drop.cancel()
        leftovers = [
            t
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        assert leftovers == [], f"orphaned tasks: {leftovers}"

    run(main())
    assert gw.closed is True


def test_persistent_handshake_failure_gives_up(monkeypatch):
    """A failing handshake must not silence the reconnect chain forever."""
    monkeypatch.setattr(G, "MAX_RECONNECT_ATTEMPTS", 3)
    ws1 = FakeWS([hello(), ready()])
    gw = make_gateway(ws1)
    gw.http.session.sockets.extend(dead_socket() for _ in range(5))

    async def main():
        await gw.connect()
        ws1.drop(code=1006)
        await gw.wait_closed()

    with pytest.raises(GatewayClosed, match="reconnect failed after 3 attempts"):
        run(main())
    assert gw.closed is True
    assert len(gw.http.session.urls) == 1 + 3  # original + 3 retries, no more


def test_all_background_tasks_stopped_on_close():
    ws = FakeWS([hello(), ready()])
    gw = make_gateway(ws)

    async def main():
        await gw.connect()
        beat, rotate, reader = gw._beat_task, gw._rotate_task, gw._reader_task
        await gw.close()
        for task in (beat, rotate, reader):
            assert task.done() or task.cancelled()
        assert gw._beat_task is None
        assert gw._reader_task is None
        assert gw._rotate_task is None

    run(main())
