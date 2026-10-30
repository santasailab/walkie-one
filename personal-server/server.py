"""One process: authenticated device/Link sockets and replaceable speech."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import json
import logging
import logging.handlers
import os
import re
import secrets
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from configuration import initialize, load, origin_url, private_json, profiles, validate
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server-runtime"))
from speech import (
    SpeechError,
    configured_speech,
    load_fallback_error_audio,
)
from turns import NORMALIZED_AGENT_FAILURES, CloudTurnRouter

CHANNEL = "personal"
MAX_FRAME = 64 * 1024
TURN_TIMEOUT = 25 * 60
MAX_RECORDING_BYTES = 24_000 * 2 * 60


def control(kind, **values):
    return json.dumps({"v": 1, "type": kind, **values}, separators=(",", ":"))


class Budget:
    def __init__(self, rate, capacity):
        self.rate, self.capacity = rate, capacity
        self.tokens, self.updated = capacity, time.monotonic()

    def take(self, amount=1):
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        self.tokens -= amount
        return self.tokens >= 0


class Peer:
    def __init__(self, socket):
        self.socket = socket
        self.lock = asyncio.Lock()
        self.messages = Budget(200, 400)
        self.bytes = Budget(128 * 1024, 256 * 1024)
        self.turns = Budget(0.1, 6)
        self.capture_started = None
        self.capture_bytes = 0
        self.device_presence = False
        self.steering = False
        self.busy_id = None

    async def send(self, frames):
        try:
            async with self.lock, asyncio.timeout(10):
                for frame in frames:
                    if isinstance(frame, str):
                        await self.socket.send_text(frame)
                    else:
                        await self.socket.send_bytes(bytes(frame))
            return True
        except (OSError, RuntimeError, TimeoutError, WebSocketDisconnect):
            return False


class PersonalServer:
    def __init__(self, config, speech, *, transcript=None):
        validate(config)
        self.config = config
        self.devices = {}
        self.link = None
        self.agent = "unavailable"
        self.failure = None
        self.membership = asyncio.Lock()
        self.pending = {}
        self.transcript = transcript
        # Limits reconnect churn even for a client that already holds a credential.
        self.connections = {
            name: Budget(0.2, 6) for name in [*config["devices"], "link"]
        }
        self.router = CloudTurnRouter(
            speech=speech,
            endpoint_state=self.endpoint_state,
            send_link=self.send_link,
            send_device=self.send_device,
            set_busy=self.publish_presence,
            fallback_error_audio=load_fallback_error_audio(),
            steering_enabled=lambda _channel: bool(self.link and self.link.steering),
            steering_target=lambda _channel: self.link.busy_id if self.link else None,
        )

    @property
    def primary(self):
        return next(iter(self.config["devices"]))

    def endpoint_state(self, _channel):
        return (self.failure or self.agent) if self.link else "link_unavailable"

    def record(self, pair, kind, text):
        if self.transcript:
            self.transcript.info(
                json.dumps(
                    {"time": time.time(), "device": pair, "event": kind, "text": text}
                )
            )

    async def send_link(self, channel, pair, payload):
        if channel != CHANNEL or pair not in self.config["devices"] or not self.link:
            return False
        message = json.loads(payload)
        is_request = message["type"] == "link.turn.request"
        if is_request:
            self.pending[message["id"]] = (pair, time.monotonic())
        sent = await self.link.send(
            [control("link.route", pair=pair, primary=pair == self.primary), payload]
        )
        if is_request:
            if sent:
                self.record(pair, "request", message["text"])
            else:
                self.pending.pop(message["id"], None)
        return sent

    async def send_device(self, channel, pair, frames):
        peer = self.devices.get(pair) if channel == CHANNEL else None
        return await peer.send(frames) if peer else False

    async def publish_presence(self, _channel=CHANNEL, _busy=False):
        agent = "busy" if self.router.working(CHANNEL) else self.agent
        frames = [
            control("peer", mac=self.link is not None),
            control(
                "presence",
                cloud=True,
                link=self.link is not None,
                agent=agent if self.link else "unavailable",
                ack_confirmations=False,
            ),
        ]
        await asyncio.gather(
            *(peer.send(frames) for peer in list(self.devices.values()))
        )

    async def device_presence(self):
        if self.link and self.link.device_presence:
            await self.link.send(
                control("link.device", pair=name, online=name in self.devices)
                for name in self.config["devices"]
            )

    async def watchdog(self):
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            for peer in list(self.devices.values()):
                if peer.capture_started is not None and now - peer.capture_started > 65:
                    with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                        await peer.socket.close(4408, "Recording timed out")
            if self.link and any(
                now - started > TURN_TIMEOUT for _, started in self.pending.values()
            ):
                with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                    await self.link.socket.close(
                        4408, "Assistant timed out; check it before retrying"
                    )

    async def connection(self, socket, role):
        # Credentials stay out of URLs; browser origins are unsupported. Native
        # firmware and Link use Authorization. Reject before accepting a socket.
        token = socket.headers.get("authorization", "").removeprefix("Bearer ")
        candidates = (
            self.config["devices"]
            if role == "device"
            else {"link": self.config["link_credential"]}
        )
        identity = next(
            (
                name
                for name, expected in candidates.items()
                if token.isascii() and hmac.compare_digest(token, expected)
            ),
            None,
        )
        if socket.query_params or socket.headers.get("origin") or identity is None:
            await socket.close(4401)
            return
        if not self.connections[identity].take():
            await socket.close(4429)
            return
        peer = Peer(socket)
        async with self.membership:
            # Reject duplicate identities instead of letting a stale socket
            # replace a fresh one or tear down its playback on disconnect.
            if identity in self.devices if role == "device" else self.link is not None:
                await socket.close(4409)
                return
            await socket.accept()
            if role == "device":
                self.devices[identity] = peer
                await self.router.device_ready(CHANNEL, identity)
            else:
                self.link = peer
                self.agent, self.failure = "unavailable", None
                await peer.send([control("link.primary", pair=self.primary)])
                await self.router.link_ready(CHANNEL)
        try:
            await self.publish_presence()
            await self.device_presence()
            await self.receive(peer, role, identity)
        except (WebSocketDisconnect, OSError, RuntimeError):
            pass
        finally:
            async with self.membership:
                if role == "device":
                    self.devices.pop(identity, None)
                    await self.router.device_gone(identity)
                else:
                    self.link = None
                    self.agent, self.failure = "unavailable", None
                    self.pending.clear()
                    await self.router.link_gone(CHANNEL)
            await self.publish_presence()
            await self.device_presence()

    async def receive(self, peer, role, identity):
        output_pair = None
        while True:
            event = await peer.socket.receive()
            if event["type"] == "websocket.disconnect":
                return
            frame = (
                event.get("text")
                if event.get("text") is not None
                else event.get("bytes", b"")
            )
            size = len(frame.encode()) if isinstance(frame, str) else len(frame)
            if (
                size > MAX_FRAME
                or not peer.messages.take()
                or not peer.bytes.take(size)
            ):
                await peer.socket.close(4429, "Message limit exceeded")
                return
            try:
                message = json.loads(frame) if isinstance(frame, str) else None
                if isinstance(frame, str) and (
                    not isinstance(message, dict)
                    or type(message.get("v")) is not int
                    or message.get("v") != 1
                ):
                    raise ValueError("Invalid message")
                if role == "device":
                    kind = message.get("type") if message else None
                    if kind in {"talk.start", "listen"}:
                        if not peer.turns.take():
                            await peer.socket.close(
                                4429, "Speech request limit exceeded"
                            )
                            return
                        if kind == "talk.start" and peer.capture_started is None:
                            peer.capture_started = time.monotonic()
                            peer.capture_bytes = 0
                    elif kind == "talk.end":
                        peer.capture_started = None
                    if isinstance(frame, bytes) and peer.capture_started is not None:
                        peer.capture_bytes += len(frame)
                        if peer.capture_bytes > MAX_RECORDING_BYTES:
                            await peer.socket.close(4408, "Recording limit exceeded")
                            return
                    if kind in {"hello", "status", "firmware.status"}:
                        # No remote registration/claim/OTA authority in this server.
                        continue
                    if not await self.router.handle_device(CHANNEL, identity, frame):
                        raise ValueError("Unsupported device message")
                else:
                    if message is None:
                        raise ValueError("Link sends text only")
                    kind = message.get("type")
                    if kind == "link.output":
                        if (
                            output_pair is not None
                            or message.get("pair") not in self.config["devices"]
                        ):
                            raise ValueError("Unknown or nested device route")
                        output_pair = message["pair"]
                    elif kind == "link.presence" and output_pair is None:
                        agent, failure = message.get("agent"), message.get("failure")
                        if agent not in {"ready", "busy", "unavailable"} or (
                            failure is not None
                            and (
                                agent != "unavailable"
                                or failure not in NORMALIZED_AGENT_FAILURES
                            )
                        ):
                            raise ValueError("Invalid assistant presence")
                        self.agent, self.failure = agent, failure
                        peer.device_presence = message.get("device_presence") is True
                        peer.steering = message.get("steering") is True
                        busy_id = message.get("busy_id")
                        if busy_id is not None and (
                            agent != "busy"
                            or not isinstance(busy_id, str)
                            or not re.fullmatch(r"[0-9a-f]{32}", busy_id)
                        ):
                            raise ValueError("Invalid assistant turn")
                        peer.busy_id = busy_id
                        await self.publish_presence()
                        await self.device_presence()
                    else:
                        if output_pair is None:
                            raise ValueError("Missing device route")
                        pair, output_pair = output_pair, None
                        pending = self.pending.get(message.get("id"))
                        if (
                            kind in {"link.turn.result", "link.turn.error"}
                            and pending
                            and pending[0] != pair
                        ):
                            raise ValueError("Reply belongs to another device")
                        handled = await self.router.handle_link(
                            CHANNEL,
                            pair,
                            message,
                            proactive_allowed=pair == self.primary,
                        )
                        if not handled:
                            raise ValueError("Unsupported Link message")
                        if kind in {"link.turn.result", "link.turn.error"} and pending:
                            self.pending.pop(message["id"], None)
                            if kind == "link.turn.result":
                                self.record(pair, "reply_generated", message["text"])
            except (ValueError, TypeError, KeyError, RecursionError):
                await peer.socket.close(4400, "Invalid protocol message")
                return


def create_app(config, speech, *, transcript=None):
    server = PersonalServer(config, speech, transcript=transcript)

    @asynccontextmanager
    async def lifespan(_app):
        watcher = asyncio.create_task(server.watchdog())
        try:
            yield
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            await server.router.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.personal = server

    @app.get("/healthz")
    async def health():
        return {"status": "running"}

    @app.websocket("/device")
    async def device(socket: WebSocket):
        await server.connection(socket, "device")

    @app.websocket("/link")
    async def link(socket: WebSocket):
        await server.connection(socket, "link")

    return app


class PrivateTranscriptHandler(logging.handlers.RotatingFileHandler):
    def _open(self):
        descriptor = os.open(
            self.baseFilename,
            os.O_APPEND | os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        return os.fdopen(descriptor, "a", encoding="utf-8")


def transcript_log(directory, enabled):
    if not enabled:
        return None
    # The entire state directory is mode 0700. Refuse symlink log destinations.
    for suffix in ("", ".1", ".2", ".3"):
        path = directory / ("transcripts.jsonl" + suffix)
        if path.is_symlink():
            raise ValueError("Transcript path must not be a symlink")
        if path.exists() and path.stat().st_mode & 0o077:
            raise ValueError("Transcript files must be private")
    logger = logging.getLogger("personal.transcripts")
    logger.propagate, logger.level = False, logging.INFO
    logger.addHandler(
        PrivateTranscriptHandler(
            directory / "transcripts.jsonl", maxBytes=1024 * 1024, backupCount=3
        )
    )
    return logger


def speech_from_config(config):
    """Use only this server's speech settings, including during setup checks."""
    for name in list(os.environ):
        if name.startswith(
            (
                "AZURE_",
                "DEEPGRAM_",
                "WALKIEONE_STT_",
                "WALKIEONE_TTS_",
                "WALKIEONE_SPEECH_",
            )
        ):
            del os.environ[name]
    os.environ.update(config["speech"])
    return configured_speech()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "init",
            "run",
            "check",
            "add-device",
            "remove-device",
            "set-origin",
            "rotate-link",
        ],
    )
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--origin", default="http://127.0.0.1:8787")
    parser.add_argument("--name")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--cert", type=Path)
    parser.add_argument("--key", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "init":
            initialize(args.directory, args.origin)
            print(
                "Created private server.json, link.json and walkie-1.json. Add your speech key to server.json."
            )
            return
        config = load(args.directory)
        if args.action in {"add-device", "remove-device", "set-origin", "rotate-link"}:
            if args.action in {"add-device", "remove-device"}:
                if (
                    not args.name
                    or not re.fullmatch(r"[a-z0-9-]{1,32}", args.name)
                    or args.name in {"link", "server"}
                ):
                    raise ValueError(
                        "Use a short lowercase name; link and server are reserved"
                    )
                if args.action == "add-device":
                    if args.name in config["devices"]:
                        raise ValueError(
                            "Device already exists; remove it before replacing its credential"
                        )
                    config["devices"][args.name] = (
                        f"v1.{args.name}.device." + secrets.token_urlsafe(32)
                    )
                else:
                    del config["devices"][args.name]
            elif args.action == "set-origin":
                config["origin"] = origin_url(args.origin)
            else:
                config["link_credential"] = (
                    config["link_credential"].rsplit(".", 1)[0]
                    + "."
                    + secrets.token_urlsafe(32)
                )
            validate(config)
            private_json(args.directory / "server.json", config, replace=True)
            profiles(args.directory, config)
            if args.action == "remove-device":
                (args.directory / f"{args.name}.json").unlink(missing_ok=True)
            print(
                "Saved. Restart the server to apply; import changed client profiles again."
            )
            return
        speech = speech_from_config(config)
        if args.action == "check":
            print(
                "Private configuration and speech settings valid. No provider/network call made."
            )
            return
        if args.host not in {"127.0.0.1", "::1"} and not (args.cert and args.key):
            raise ValueError("Non-loopback listeners require --cert and --key")
        app = create_app(
            config,
            speech,
            transcript=transcript_log(args.directory, config["transcripts"]),
        )
        print(
            f"Personal server listening on {args.host}:{args.port}; /healthz checks the process. Keep this terminal open."
        )
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            access_log=False,
            log_level="warning",
            proxy_headers=False,
            ws_max_size=MAX_FRAME,
            ws_max_queue=8,
            ws_per_message_deflate=False,
            ssl_certfile=str(args.cert) if args.cert else None,
            ssl_keyfile=str(args.key) if args.key else None,
        )
    except (OSError, ValueError, TypeError, KeyError, SpeechError):
        parser.exit(
            1,
            "Setup failed. Check private file permissions, configuration, speech settings and arguments; secrets withheld.\n",
        )


if __name__ == "__main__":
    main()
