"""Exercise the personal server through real sockets, without a control plane."""

from __future__ import annotations

import asyncio
import json
import secrets
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest
import websockets
from fastapi import Request
from fastapi.testclient import TestClient

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "personal-server"))
from configuration import initialize, load, origin_url, private_json, validate
from rehearse import RehearsalSpeech, message_of, rehearsal, running_server
from server import control, create_app, transcript_log


def configuration(tmp_path):
    config = initialize(tmp_path / "state", "https://personal.example.org")
    config["devices"]["walkie-2"] = "v1.walkie-2.device." + secrets.token_urlsafe(32)
    return config


async def dial(port, path, credential, **kwargs):
    return await websockets.connect(
        f"ws://127.0.0.1:{port}/{path}",
        additional_headers={"Authorization": credential},
        **kwargs,
    )


async def request(link):
    async with asyncio.timeout(3):
        route = None
        while True:
            message = json.loads(await link.recv())
            if message["type"] == "link.route":
                route = message
            elif message["type"] == "link.turn.request":
                return route, message


async def reply(link, pair, turn, **extra):
    await link.send(control("link.output", pair=pair))
    await link.send(
        control(
            "link.turn.result", id=turn["id"], text="Done.", needs_answer=False, **extra
        )
    )


async def speak(device):
    await device.send(control("talk.start"))
    await device.send(b"\0\0" * 2400)
    await device.send(control("talk.end"))


async def no_audio(device):
    while True:
        try:
            frame = await asyncio.wait_for(device.recv(), 0.1)
        except TimeoutError:
            return
        assert isinstance(frame, str)
        assert json.loads(frame)["type"] in {"peer", "presence"}


def test_private_configuration_and_profiles(tmp_path):
    state = tmp_path / "private"
    original = initialize(state, "https://walkie.example.org")
    assert load(state) == original
    assert state.stat().st_mode & 0o077 == 0
    for path in state.iterdir():
        assert path.stat().st_mode & 0o077 == 0
    profile = json.loads((state / "link.json").read_text())
    assert set(profile) == {"url", "credential"}
    assert profile["credential"] != original["devices"]["walkie-1"]
    # Native apps parse both route fields as UUIDs, unlike the looser CLI parser.
    for route_id in profile["credential"].split(".")[1:3]:
        assert uuid.UUID(hex=route_id).hex == route_id
    with pytest.raises(FileExistsError):
        initialize(state, "https://other.example.org")
    (state / "server.json").chmod(0o644)
    with pytest.raises(ValueError):
        load(state)


@pytest.mark.parametrize(
    "origin",
    [
        "http://public.example.org",
        "https://user:secret@example.org",
        "https://example.org?secret=x",
        "https://example.org/device",
        "https://example.org/#fragment",
    ],
)
def test_unsafe_origin_rejected(origin):
    with pytest.raises(ValueError):
        origin_url(origin)


def test_config_symlink_rejected(tmp_path):
    state = tmp_path / "private"
    config = initialize(state, "https://example.org")
    target = tmp_path / "secret.json"
    private_json(target, config)
    (state / "server.json").unlink()
    (state / "server.json").symlink_to(target)
    with pytest.raises(OSError):
        load(state)


def test_authority_and_unsupported_http_routes(tmp_path):
    config = configuration(tmp_path)
    with TestClient(create_app(config, RehearsalSpeech())) as client:
        assert client.get("/healthz").json() == {"status": "running"}
        for path in ("/docs", "/v1/me", "/internal/relay/active-links", "/server.json"):
            assert client.get(path).status_code == 404
        for path, token, extra in [
            ("/device", config["link_credential"], {}),
            ("/link", config["devices"]["walkie-1"], {}),
            ("/device", "unknown", {}),
            ("/device?token=x", config["devices"]["walkie-1"], {}),
            (
                "/device",
                config["devices"]["walkie-1"],
                {"Origin": "https://untrusted.example"},
            ),
        ]:
            from starlette.websockets import WebSocketDisconnect

            with (
                pytest.raises(WebSocketDisconnect),
                client.websocket_connect(
                    path, headers={"Authorization": token, **extra}
                ),
            ):
                pytest.fail("Unauthorized socket accepted")


def test_two_devices_acknowledgments_and_wrong_route(tmp_path):
    config = configuration(tmp_path)
    app = create_app(config, RehearsalSpeech())

    async def exercise():
        async with (
            running_server(app) as port,
            await dial(port, "link", config["link_credential"]) as link,
        ):
            await message_of(link, "link.primary")
            await link.send(control("link.presence", agent="ready"))
            async with (
                await dial(port, "device", config["devices"]["walkie-1"]) as first,
                await dial(port, "device", config["devices"]["walkie-2"]) as second,
            ):
                for device, name, other in [
                    (first, "walkie-1", second),
                    (second, "walkie-2", first),
                ]:
                    await speak(device)
                    route, turn = await request(link)
                    assert route["pair"] == name
                    await reply(link, name, turn)
                    start = await message_of(device, "audio.start")
                    await message_of(device, "audio.end")
                    await message_of(device, "digest")
                    await no_audio(other)
                    # Another device cannot acknowledge this one's playback.
                    await other.send(control("ack", id=start["id"]))
                    await asyncio.sleep(0.02)
                    assert name in app.state.personal.router._playback
                    await device.send(control("ack", id=start["id"]))
                    assert (await message_of(device, "digest"))["missed"] == 0
                    await message_of(link, "link.turn.ack")
                await speak(first)
                _, turn = await request(link)
                await reply(link, "walkie-2", turn)
                with pytest.raises(websockets.ConnectionClosed):
                    while True:
                        await asyncio.wait_for(link.recv(), 2)

    asyncio.run(exercise())


def test_disconnect_rejects_stale_audio_and_restores_connections(tmp_path):
    config = configuration(tmp_path)
    app = create_app(config, RehearsalSpeech())

    async def exercise():
        async with (
            running_server(app) as port,
            await dial(port, "link", config["link_credential"]) as link,
        ):
            await message_of(link, "link.primary")
            await link.send(control("link.presence", agent="ready"))
            first = await dial(port, "device", config["devices"]["walkie-1"])
            await speak(first)
            _, turn = await request(link)
            await first.close()
            async with asyncio.timeout(2):
                while "walkie-1" in app.state.personal.devices:
                    await asyncio.sleep(0.01)
            async with await dial(
                port, "device", config["devices"]["walkie-1"]
            ) as replacement:
                await reply(link, "walkie-1", turn)
                await no_audio(replacement)
                await speak(replacement)
                _, next_turn = await request(link)
                await reply(link, "walkie-1", next_turn)
                await message_of(replacement, "audio.start")

    asyncio.run(exercise())


def test_malformed_route_and_connection_limits(tmp_path):
    config = configuration(tmp_path)
    app = create_app(config, RehearsalSpeech())

    async def exercise():
        async with running_server(app) as port:
            async with await dial(
                port, "device", config["devices"]["walkie-1"]
            ) as device:
                with pytest.raises(websockets.InvalidStatus):
                    await dial(port, "device", config["devices"]["walkie-1"])
                await device.send(b"x" * 65537)
                with pytest.raises(websockets.ConnectionClosed):
                    while True:
                        await device.recv()
            async with await dial(port, "link", config["link_credential"]) as link:
                await link.send(control("link.output", pair="foreign-device"))
                with pytest.raises(websockets.ConnectionClosed):
                    while True:
                        await link.recv()

    asyncio.run(exercise())


def test_real_link_without_external_dns_or_database(tmp_path, monkeypatch):
    lookup = socket.getaddrinfo

    def local_only(host, *args, **kwargs):
        assert host in {"localhost", "127.0.0.1", "::1", None}
        return lookup(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", local_only)
    with tempfile.TemporaryDirectory(
        prefix="walkie-rehearsal-", dir="/tmp"
    ) as directory:
        asyncio.run(rehearsal(Path(directory)))


def test_two_real_links_share_a_computer_without_replacing_its_native_socket():
    sys.path.insert(0, str(ROOT / "link-runtime"))
    from proactive import LocalSendReceipt, LocalSendServer

    async def exercise(directory):
        home = directory / "home"
        home.mkdir()
        first, second = directory / "first", directory / "second"
        first.mkdir()
        second.mkdir()
        native_socket = home / ".relay" / "ipc" / "send.sock"

        async def native_send(_request):
            return LocalSendReceipt.rejected("cloud_unavailable")

        async with LocalSendServer(native_socket, native_send):
            inode = native_socket.stat().st_ino
            await asyncio.gather(
                rehearsal(first, private_home=home),
                rehearsal(second, private_home=home),
            )
            assert native_socket.stat().st_ino == inode
            reader, writer = await asyncio.open_unix_connection(native_socket)
            writer.close()
            await writer.wait_closed()

    with tempfile.TemporaryDirectory(prefix="walkie-links-", dir="/tmp") as directory:
        asyncio.run(exercise(Path(directory)))


def test_real_link_and_https_speech_contract_without_provider_credentials(tmp_path):
    import urllib.request

    from fastapi import FastAPI, Response
    from speech import (
        HTTPSpeechAdapter,
        SpeechError,
        _NoSpeechRedirect,
        load_fallback_error_audio,
    )
    from tls_fixture import tls_contexts

    _, trusted = tls_contexts(tmp_path)
    token = secrets.token_urlsafe(32)
    heard, spoken = [], []
    speech_app = FastAPI()

    @speech_app.post("/v1/transcribe")
    async def transcribe(request: Request):
        assert request.headers["Authorization"] == "Bearer " + token
        heard.append(await request.body())
        return {"text": "Say ready."}

    @speech_app.post("/v1/synthesize")
    async def synthesize(request: Request):
        assert request.headers["Authorization"] == "Bearer " + token
        spoken.append(await request.json())
        return Response(load_fallback_error_audio(), media_type="audio/mpeg")

    async def exercise(directory):
        async with running_server(
            speech_app,
            ssl_certfile=str(tmp_path / "relay-cert.pem"),
            ssl_keyfile=str(tmp_path / "relay-key.pem"),
        ) as port:
            url = f"https://localhost:{port}/v1"
            # The default client must reject this test-only certificate.
            with pytest.raises(SpeechError):
                await asyncio.to_thread(
                    HTTPSpeechAdapter(url, token).transcribe, b"\0\0"
                )
            opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=trusted), _NoSpeechRedirect()
            ).open
            await rehearsal(
                directory, speech=HTTPSpeechAdapter(url, token, opener=opener)
            )
        assert heard == [b"\0\0" * 2400]
        assert spoken == [{"text": "Ready.", "voice": None}]

    with tempfile.TemporaryDirectory(prefix="walkie-https-", dir="/tmp") as directory:
        asyncio.run(exercise(Path(directory)))


def test_cli_add_remove_and_refuse_missing_speech(tmp_path):
    state = tmp_path / "private"
    initialize(state, "https://example.org")

    def run(*args):
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "personal-server/server.py"),
                *args,
                "--directory",
                str(state),
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

    assert run("check").returncode == 1
    for reserved in ("server", "link"):
        assert run("add-device", "--name", reserved).returncode == 1
    assert "walkie-1" in load(state)["devices"]
    assert run("add-device", "--name", "second").returncode == 0
    assert "second" in load(state)["devices"]
    assert run("add-device", "--name", "second").returncode == 1
    assert run("remove-device", "--name", "second").returncode == 0
    assert not (state / "second.json").exists()
    assert run("remove-device", "--name", "walkie-1").returncode == 1
    old = load(state)["link_credential"]
    assert run("rotate-link").returncode == 0
    assert load(state)["link_credential"] != old
    validate(load(state))


def test_tls_requires_trusted_certificate_and_delivers_audio(tmp_path):
    from tls_fixture import tls_contexts

    _, trusted = tls_contexts(tmp_path)
    config = configuration(tmp_path)
    app = create_app(config, RehearsalSpeech())

    async def exercise():
        import ssl

        async with running_server(
            app,
            ssl_certfile=str(tmp_path / "relay-cert.pem"),
            ssl_keyfile=str(tmp_path / "relay-key.pem"),
        ) as port:
            with pytest.raises(ssl.SSLCertVerificationError):
                await websockets.connect(f"wss://localhost:{port}/device")
            async with websockets.connect(
                f"wss://localhost:{port}/link",
                ssl=trusted,
                additional_headers={"Authorization": config["link_credential"]},
            ) as link:
                await message_of(link, "link.primary")
                await link.send(control("link.presence", agent="ready"))
                async with websockets.connect(
                    f"wss://localhost:{port}/device",
                    ssl=trusted,
                    additional_headers={"Authorization": config["devices"]["walkie-1"]},
                ) as device:
                    await speak(device)
                    route, turn = await request(link)
                    await reply(link, route["pair"], turn)
                    await message_of(device, "audio.start")
                    await message_of(device, "audio.end")

    asyncio.run(exercise())


def test_busy_and_unsupported_proactive_delivery_are_explicit(tmp_path):
    speech = RehearsalSpeech()
    config = configuration(tmp_path)
    app = create_app(config, speech)

    async def exercise():
        async with (
            running_server(app) as port,
            await dial(port, "link", config["link_credential"]) as link,
        ):
            await message_of(link, "link.primary")
            await link.send(control("link.presence", agent="ready"))
            async with (
                await dial(port, "device", config["devices"]["walkie-1"]) as first,
                await dial(port, "device", config["devices"]["walkie-2"]) as second,
            ):
                await speak(first)
                _, turn = await request(link)
                await speak(second)
                await message_of(second, "audio.end")
                assert "Your coding agent is busy." in speech.spoken
                await reply(link, "walkie-1", turn)
                await message_of(first, "audio.end")
                await link.send(control("link.output", pair="walkie-1"))
                await link.send(
                    control(
                        "link.proactive.send",
                        id="a" * 32,
                        idempotency_key="test",
                        text="Notification",
                    )
                )
                receipt = await message_of(link, "link.proactive.receipt")
                assert receipt["status"] == "rejected"

    asyncio.run(exercise())


def test_transcripts_are_optional_private_rotated_and_reject_symlinks(tmp_path):
    config = configuration(tmp_path)
    state = tmp_path / "state"
    assert transcript_log(state, False) is None
    logger = transcript_log(state, True)
    server = create_app(config, RehearsalSpeech(), transcript=logger).state.personal
    try:
        server.record("walkie-1", "request", "Hello")
        path = state / "transcripts.jsonl"
        assert json.loads(path.read_text())["text"] == "Hello"
        assert path.stat().st_mode & 0o077 == 0
        assert logger.handlers[0].maxBytes == 1024 * 1024
        assert logger.handlers[0].backupCount == 3
    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
    path.unlink()
    path.symlink_to(state / "server.json")
    with pytest.raises(ValueError):
        transcript_log(state, True)


@pytest.mark.parametrize("failed_speech", [False, True])
def test_live_only_permission_expiry_and_failed_speech_receipts(
    tmp_path, failed_speech
):
    from speech import SpeechError

    class Speech(RehearsalSpeech):
        def synthesize(self, text, voice=None):
            if failed_speech:
                raise SpeechError("test synthesis unavailable")
            return super().synthesize(text, voice)

    config = configuration(tmp_path)
    app = create_app(config, Speech())

    async def exercise():
        async with (
            running_server(app) as port,
            await dial(port, "link", config["link_credential"]) as link,
        ):
            await message_of(link, "link.primary")
            await link.send(control("link.presence", agent="ready"))
            async with await dial(
                port, "device", config["devices"]["walkie-1"]
            ) as device:
                await speak(device)
                _, turn = await request(link)
                await link.send(control("link.output", pair="walkie-1"))
                await link.send(
                    control(
                        "link.turn.result",
                        id=turn["id"],
                        text="Allow this action?",
                        needs_answer=True,
                        provider_interaction_ttl_seconds=1,
                    )
                )
                start = await message_of(device, "audio.start")
                await message_of(device, "audio.end")
                digest = await message_of(device, "digest")
                if failed_speech:
                    assert digest["needs_answer"] is False
                    await device.send(control("ack", id=start["id"]))
                    assert (await message_of(device, "digest"))["missed"] == 0
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(link.recv(), 0.15)
                else:
                    assert digest["needs_answer"] is True
                    retraction = await message_of(device, "audio.end")
                    assert retraction["id"] == start["id"]
                    assert retraction["complete"] is False
                    await device.send(control("ack", id=start["id"]))
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(link.recv(), 0.15)

    asyncio.run(exercise())


def test_same_device_steers_current_turn_and_another_device_stays_busy(tmp_path):
    speech = RehearsalSpeech()
    config = configuration(tmp_path)
    app = create_app(config, speech)

    async def exercise():
        async with (
            running_server(app) as port,
            await dial(port, "link", config["link_credential"]) as link,
        ):
            await message_of(link, "link.primary")
            await link.send(control("link.presence", agent="ready", steering=True))
            async with (
                await dial(port, "device", config["devices"]["walkie-1"]) as first,
                await dial(port, "device", config["devices"]["walkie-2"]) as second,
            ):
                await speak(first)
                _, turn = await request(link)
                await speak(first)
                steer = await message_of(link, "link.turn.steer")
                assert steer["turn_id"] == turn["id"]
                await link.send(control("link.output", pair="walkie-1"))
                await link.send(
                    control(
                        "link.turn.steered",
                        id=steer["id"],
                        turn_id=turn["id"],
                        status="accepted",
                    )
                )
                await speak(second)
                await message_of(second, "audio.end")
                assert "Your coding agent is busy." in speech.spoken
                await reply(link, "walkie-1", turn)
                start = await message_of(first, "audio.start")
                await message_of(first, "audio.end")
                await first.send(control("ack", id=start["id"]))
                while (await message_of(first, "digest"))["missed"]:
                    pass
                await speak(first)
                route, next_turn = await request(link)
                assert route["pair"] == "walkie-1"
                assert next_turn["id"] != turn["id"]
                await reply(link, "walkie-1", next_turn)
                await message_of(first, "audio.end")

    asyncio.run(exercise())


@pytest.mark.parametrize("role", ["device", "link"])
@pytest.mark.parametrize(
    "payload", ['{"v":true,"type":"hello"}', "[" * 2000 + "]" * 2000]
)
def test_malformed_protocol_closes_only_the_offending_socket(tmp_path, role, payload):
    config = configuration(tmp_path)
    app = create_app(config, RehearsalSpeech())

    async def exercise():
        credential = (
            config["devices"]["walkie-1"]
            if role == "device"
            else config["link_credential"]
        )
        async with running_server(app) as port:
            async with await dial(port, role, credential) as peer:
                await peer.send(payload)
                with pytest.raises(websockets.ConnectionClosedError) as closed:
                    while True:
                        await asyncio.wait_for(peer.recv(), 3)
                assert closed.value.rcvd.code == 4400
            async with await dial(
                port, "device", config["devices"]["walkie-2"]
            ) as healthy:
                assert (await message_of(healthy, "presence"))["cloud"] is True

    asyncio.run(exercise())


def test_recording_byte_limit_is_enforced_independently_of_shared_router(
    tmp_path, monkeypatch
):
    import server

    monkeypatch.setattr(server, "MAX_RECORDING_BYTES", 1000)
    config = configuration(tmp_path)
    app = create_app(config, RehearsalSpeech())

    async def exercise():
        async with (
            running_server(app) as port,
            await dial(port, "link", config["link_credential"]) as link,
        ):
            await message_of(link, "link.primary")
            await link.send(control("link.presence", agent="ready"))
            async with await dial(
                port, "device", config["devices"]["walkie-1"]
            ) as device:
                await message_of(device, "presence")
                await device.send(control("talk.start"))
                await device.send(b"\0" * 1002)
                with pytest.raises(websockets.ConnectionClosedError) as closed:
                    while True:
                        await asyncio.wait_for(device.recv(), 3)
                assert closed.value.rcvd.code == 4408

    asyncio.run(exercise())
