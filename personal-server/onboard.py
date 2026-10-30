"""Private speech setup and bounded connectivity checks; no installer or hardware access."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
import warnings
from pathlib import Path

from configuration import initialize, load, origin_url, private_json, validate
from server import speech_from_config
from speech import SpeechError
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException


class SetupError(Exception):
    """Safe, actionable messages that never include input or provider responses."""


def private_directory(directory: Path) -> Path:
    if directory.is_symlink():
        raise SetupError("Choose a private directory, not a symbolic link.")
    directory = directory.expanduser().resolve()
    if any((parent / ".git").exists() for parent in (directory, *directory.parents)):
        raise SetupError(
            "Choose a directory outside every Git checkout, such as ~/walkie-private."
        )
    return directory


def configure(directory: Path, provider: str) -> None:
    directory = private_directory(directory)
    existing = load(directory) if directory.exists() else None
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise SetupError(
            "Run configure yourself in Terminal; secret entry requires an interactive terminal."
        )
    print(
        f"Configure {provider} for both speech directions. Existing device credentials stay unchanged."
    )
    speech = {
        "WALKIEONE_STT_PROVIDER": provider,
        "WALKIEONE_TTS_PROVIDER": provider,
    }
    if existing and all(existing["speech"].get(name) == provider for name in speech):
        speech = dict(existing["speech"])
    if provider == "http":
        endpoint = input(
            "Your HTTPS speech service URL (no secret; Enter keeps saved URL): "
        ).strip()
        speech["WALKIEONE_SPEECH_URL"] = endpoint or speech.get(
            "WALKIEONE_SPEECH_URL", ""
        )
    key = "DEEPGRAM_API_KEY" if provider == "deepgram" else "WALKIEONE_SPEECH_TOKEN"
    previous = existing["speech"].get(key, "") if existing else ""
    prompt = (
        "Speech API key/token (hidden"
        + ("; Enter keeps the saved value" if previous else "")
        + "): "
    )
    # getpass must never fall back to echoed input on a terminal that cannot hide it.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        secret = getpass.getpass(prompt) or previous
    if not secret or len(secret) > 4096 or any(ord(c) < 33 for c in secret):
        raise SetupError(
            "Key/token is missing or contains whitespace. Nothing was saved."
        )
    speech[key] = secret
    speech_from_config(
        {"speech": speech}
    )  # Validate locally before creating or changing files.
    config = existing or initialize(directory, "http://127.0.0.1:8787")
    config["speech"] = speech
    validate(config)
    private_json(directory / "server.json", config, replace=True)
    print(
        "PASS: speech settings saved privately. No provider request made. Restart a running server to apply."
    )


def check_speech(config: dict, approved: bool) -> None:
    if not approved:
        raise SetupError(
            "This sends two speech-provider requests and may cost money. Obtain approval, then use --approve-provider-requests."
        )
    speech = speech_from_config(config)
    for label, call in (
        ("speech-to-text", lambda: speech.transcribe(b"\0\0" * 2400)),
        (
            "text-to-speech",
            lambda: speech.synthesize("Your Walkie One server is ready."),
        ),
    ):
        try:
            call()
        except SpeechError:
            raise SetupError(
                f"{label} check failed. Check the provider account, key permissions, balance and selected model; response withheld."
            ) from None
        print(f"PASS: {label} API accepted its test request.")
    print(
        "Used 0.1 seconds of silence and one fixed sentence. This does not test recognition accuracy, playback or hardware."
    )


class DirectConnect(connect):
    def process_redirect(self, exc):
        # A dashboard/login/redirect is not the configured device endpoint.
        return exc


async def check_network(
    config: dict, *, local_port: int | None = None, ssl_context=None
) -> dict:
    origin = origin_url(config["origin"])
    if local_port is not None:
        if not 1 <= local_port <= 65535:
            raise SetupError("Use a local port between 1 and 65535.")
        origin = f"http://127.0.0.1:{local_port}"
    elif not origin.startswith("https://"):
        raise SetupError(
            "Save the public HTTPS address with set-origin first, or use --local-port 8787."
        )
    base = origin.replace("https://", "wss://").replace("http://", "ws://")
    options = dict(
        open_timeout=10, close_timeout=2, max_size=65536, max_queue=8, proxy=None
    )
    if ssl_context is not None:
        options["ssl"] = (
            ssl_context  # Test injection only; no CLI option to bypass certificate checks.
        )
    async with asyncio.timeout(20):
        for path in ("/device", "/link"):
            try:
                async with DirectConnect(base + path, **options):
                    raise SetupError(
                        "Endpoint accepted an unauthenticated connection. Stop public access and check server authentication."
                    )
            except InvalidStatus as error:
                if error.response.status_code not in {401, 403}:
                    raise SetupError(
                        "Endpoint returned a redirect or unexpected status. Check the address and tunnel routing; no login page may sit in front of WebSockets."
                    ) from None
        credential = next(iter(config["devices"].values()))
        async with DirectConnect(
            base + "/device",
            additional_headers={"Authorization": credential},
            **options,
        ) as ws:
            for _ in range(8):
                message = json.loads(await ws.recv())
                if not isinstance(message, dict):
                    break
                if (
                    message.get("type") == "presence"
                    and message.get("v") == 1
                    and message.get("cloud") is True
                ):
                    return message
    raise SetupError("Server connected but did not send the expected presence message.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["configure", "check-speech", "check-network"]
    )
    parser.add_argument(
        "--directory", type=Path, default=Path.home() / "walkie-private"
    )
    parser.add_argument("--provider", choices=["deepgram", "http"], default="deepgram")
    parser.add_argument("--approve-provider-requests", action="store_true")
    parser.add_argument("--local-port", type=int)
    args = parser.parse_args()
    try:
        if args.action == "configure":
            configure(args.directory, args.provider)
        elif args.action == "check-speech":
            check_speech(load(args.directory), args.approve_provider_requests)
        else:
            presence = asyncio.run(
                check_network(load(args.directory), local_port=args.local_port)
            )
            print(
                "PASS: WebSocket connected with a device credential; unauthenticated device and Link connections were rejected."
            )
            print(
                "Link: "
                + ("connected" if presence.get("link") is True else "not connected yet")
            )
            print(
                "This checks routing and authentication from this computer, not cellular, speech or assistant execution."
            )
    except SetupError as error:
        parser.exit(1, f"Setup: {error}\n")
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        SpeechError,
        WebSocketException,
        getpass.GetPassWarning,
        EOFError,
    ):
        parser.exit(
            1,
            "Setup check failed. Check private file permissions, speech settings, the running server, tunnel address and TLS certificate. Details and secrets withheld.\n",
        )
    except KeyboardInterrupt:
        parser.exit(1, "Setup cancelled.\n")


if __name__ == "__main__":
    main()
