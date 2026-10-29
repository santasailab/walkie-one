"""Status reaches the native window by push, and a moved Device stops the Link."""

import asyncio
import json
import sys
from pathlib import Path

import websockets
from websockets.frames import Close

sys.path.insert(0, str(Path(__file__).parents[1]))

import link


class _Relay:
    def __init__(self):
        self.sent = []
        self.incoming = asyncio.Queue()

    async def send(self, frame):
        self.sent.append(frame)

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.incoming.get()
        if frame is None:
            raise StopAsyncIteration
        return frame


class _Window:
    def __init__(self, token):
        self.sent = asyncio.Queue()
        self.request = type(
            "Request", (), {"path": f"/events?auth={token}", "headers": {}}
        )()

    async def send(self, frame):
        await self.sent.put(json.loads(frame))

    async def close(self, code, reason):
        raise AssertionError(f"events socket closed: {code} {reason}")

    async def wait_closed(self):
        await asyncio.Future()


def test_device_presence_is_pushed_and_unknown_while_disconnected(monkeypatch):
    monkeypatch.setattr(link, "_blocked_apps", lambda: set())

    async def exercise():
        raw = _Relay()
        mux = link._LinkMultiplexer(raw)
        run = asyncio.create_task(mux.run())
        await raw.incoming.put(
            json.dumps({"v": 1, "type": "link.primary", "pair": "pair-a"})
        )
        await raw.incoming.put(
            json.dumps(
                {"v": 1, "type": "link.device", "pair": "pair-a", "online": True}
            )
        )
        while link._pair_online is None:
            await asyncio.sleep(0)
        assert (await link._status_snapshot())["hardware"] is True
        await raw.incoming.put(
            json.dumps(
                {"v": 1, "type": "link.device", "pair": "pair-a", "online": False}
            )
        )
        while link._pair_online.get("pair-a"):
            await asyncio.sleep(0)
        assert (await link._status_snapshot())["hardware"] is False
        await raw.incoming.put(None)
        await run
        # Presence belongs to one relay connection; the next one pushes again.
        assert link._pair_online is None

    asyncio.run(exercise())


def test_events_socket_pushes_only_when_status_changes(monkeypatch):
    monkeypatch.setattr(link, "AUTH", "local-token")
    monkeypatch.setattr(link, "_blocked_apps", lambda: set())
    monkeypatch.setattr(link, "_pair_online", {})

    async def exercise():
        link._status_loop = asyncio.get_running_loop()
        link._status_changed = None
        window = _Window("local-token")
        stream = asyncio.create_task(link._local_agent_socket(window))
        first = await asyncio.wait_for(window.sent.get(), 2)
        assert first["cloud"]["state"] == link._cloud["state"]

        link._cloud_state("moved", "Device moved to another connection")
        pushed = await asyncio.wait_for(window.sent.get(), 2)
        assert pushed["cloud"]["state"] == "moved"
        # Nothing changed, so nothing more is sent.
        link._notify()
        await asyncio.sleep(0.05)
        assert window.sent.empty()
        stream.cancel()
        link._status_loop = None

    asyncio.run(exercise())


def test_a_moved_device_stops_the_link_instead_of_reconnecting(monkeypatch):
    attempts = []

    def connect(*_args, **_kwargs):
        attempts.append(1)
        raise websockets.exceptions.ConnectionClosedError(
            Close(4409, "Link is no longer active"), None
        )

    monkeypatch.setattr(websockets, "connect", connect)
    monkeypatch.setattr(link, "CLOUD_URL", "wss://relay.example/link")
    monkeypatch.setattr(link, "RELAY_TOKEN", "v2.credential")

    asyncio.run(asyncio.wait_for(link.relay_client(), 2))
    assert attempts == [1]
    assert link._cloud["state"] == "moved"


def test_restart_refills_activity_from_spoken_history(monkeypatch):
    monkeypatch.setattr(link, "_activity", link.collections.deque(maxlen=64))
    link._seed_activity(
        [
            "100\tdevice\theard\tWhat was the test word?",
            "torn line",
            "101\tchannel:alpha\tsaid\tThe test word is River.",
        ]
    )
    assert [(e["at"], e["kind"], e["text"]) for e in link._activity] == [
        (100.0, "heard", "What was the test word?"),
        (101.0, "spoke", "The test word is River."),
    ]


def test_native_status_socket_closure_does_not_wait_for_another_update(monkeypatch):
    monkeypatch.setattr(link, "AUTH", "local-token")
    monkeypatch.setattr(link, "_blocked_apps", lambda: set())

    async def exercise():
        monkeypatch.setattr(link, "_status_loop", asyncio.get_running_loop())
        monkeypatch.setattr(link, "_status_changed", None)
        async with websockets.serve(link._local_agent_socket, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with websockets.connect(
                f"ws://127.0.0.1:{port}/events?auth=local-token"
            ) as events:
                assert "agent" in json.loads(await events.recv())

    asyncio.run(asyncio.wait_for(exercise(), 3))
