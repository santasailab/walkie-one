"""Local-only rehearsal: real personal server + real Link, simulated speech/device.

No provider account, company service, database, public listener or hardware.
The fixture assistant executes no requested commands.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

import uvicorn
import websockets
from configuration import initialize, private_json
from server import ROOT, control, create_app, load_fallback_error_audio


class RehearsalSpeech:
    def __init__(self):
        self.heard = []
        self.spoken = []

    def transcribe(self, pcm):
        self.heard.append(pcm)
        return "Say ready."

    def synthesize(self, text, voice=None):
        self.spoken.append(text)
        return load_fallback_error_audio()


@contextlib.asynccontextmanager
async def running_server(app, **kwargs):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
            proxy_headers=False,
            ws_max_size=65536,
            ws_max_queue=8,
            **kwargs,
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("Local server did not start")
                await asyncio.sleep(0.01)
        yield port
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 10)
        finally:
            listener.close()


async def message_of(ws, kind):
    async with asyncio.timeout(15):
        while True:
            frame = await ws.recv()
            if isinstance(frame, str):
                message = json.loads(frame)
                if message.get("type") == kind:
                    return message


async def rehearsal(directory):
    state = directory / "state"
    config = initialize(state, "http://127.0.0.1:8787")
    speech = RehearsalSpeech()
    app = create_app(config, speech)
    # Real Link and custom adapter, with a harmless fixture executable.
    assistant = directory / "assistant.py"
    assistant.write_text(
        'import json, sys\njson.load(sys.stdin)\nprint(json.dumps({"text":"Ready.","session_id":"rehearsal","needs_answer":False}))\n'
    )
    command = directory / "command.json"
    private_json(command, {"unused": True})
    command.write_text(json.dumps([sys.executable, str(assistant)]))
    private_home = directory / "home"
    private_home.mkdir()
    async with running_server(app) as port:
        profile = state / "link.json"
        private_json(
            profile,
            {
                "url": f"ws://127.0.0.1:{port}/link",
                "credential": config["link_credential"],
            },
            replace=True,
        )
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(private_home),
            "LINK_PORT": "0",
            "PYTHONUNBUFFERED": "1",
        }
        log = directory / "link.log"
        with log.open("wb") as output:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(ROOT / "link-runtime" / "independent.py"),
                "--profile",
                str(profile),
                "--agent",
                "custom",
                "--command-file",
                str(command),
                "--development",
                stdout=output,
                stderr=output,
                env=environment,
            )
            try:
                async with asyncio.timeout(15):
                    while app.state.personal.agent != "ready":
                        if process.returncode is not None:
                            raise RuntimeError(
                                "Fixture Link exited; inspect private rehearsal log"
                            )
                        await asyncio.sleep(0.05)
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/device",
                    additional_headers={"Authorization": config["devices"]["walkie-1"]},
                ) as device:
                    await message_of(device, "presence")
                    await device.send(control("hello", autoplay=False, preload=True))
                    await device.send(control("talk.start"))
                    pcm = b"\x00\x00" * 2400
                    await device.send(pcm)
                    await device.send(control("talk.end"))
                    start = await message_of(device, "audio.start")
                    received = bytearray()
                    async with asyncio.timeout(15):
                        while True:
                            frame = await device.recv()
                            if isinstance(frame, bytes):
                                received.extend(frame)
                            elif json.loads(frame).get("type") == "audio.end":
                                break
                    assert bytes(received) == load_fallback_error_audio()
                    assert speech.heard == [pcm] and "Ready." in speech.spoken
                    await device.send(control("ack", id=start["id"]))
                    empty = await message_of(device, "digest")
                    while empty["missed"]:
                        empty = await message_of(device, "digest")
                    assert empty["missed"] == 0
                print(
                    "PASS: simulated microphone → speech fixture → real Link/custom assistant → speech fixture → simulated playback/ack."
                )
                print(
                    "No company service, database or provider credentials used. Real speech, TLS/tunnel and cellular hardware still need qualification."
                )
            finally:
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 10)
                    except TimeoutError:
                        process.kill()
                        await process.wait()


def main():
    with tempfile.TemporaryDirectory(prefix="walkie-personal-") as directory:
        asyncio.run(rehearsal(Path(directory)))


if __name__ == "__main__":
    main()
