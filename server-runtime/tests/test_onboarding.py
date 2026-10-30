"""First-time setup, secret entry and public-route checks without external services."""

import asyncio
import getpass
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, WebSocket
from starlette.responses import RedirectResponse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "personal-server"))
import onboard
from configuration import initialize, load, private_json
from rehearse import RehearsalSpeech, running_server
from server import create_app
from speech import SpeechError
from tls_fixture import tls_contexts


def interactive(monkeypatch, secret):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(getpass, "getpass", lambda _: secret)


def test_configure_private_without_json_editing_and_preserve_identity(
    tmp_path, monkeypatch, capsys
):
    secret = secrets.token_urlsafe(32)
    interactive(monkeypatch, secret)
    directory = tmp_path / "private"
    onboard.configure(directory, "deepgram")
    first = load(directory)
    assert first["speech"]["DEEPGRAM_API_KEY"] == secret
    assert directory.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in directory.iterdir())
    first["speech"]["DEEPGRAM_TTS_MODEL"] = "aura-2-thalia-en"
    first["origin"] = "https://example.org"
    private_json(directory / "server.json", first, replace=True)
    profiles = {path.name: path.read_bytes() for path in directory.glob("*.json")}
    interactive(monkeypatch, "")
    onboard.configure(directory, "deepgram")
    assert load(directory) == first
    assert {
        path.name: path.read_bytes() for path in directory.glob("*.json")
    } == profiles
    assert secret not in capsys.readouterr().out


def test_switch_provider_validates_before_saving(tmp_path, monkeypatch):
    interactive(monkeypatch, secrets.token_urlsafe(32))
    directory = tmp_path / "private"
    onboard.configure(directory, "deepgram")
    first = load(directory)
    monkeypatch.setattr("builtins.input", lambda _: "http://example.org")
    with pytest.raises(SpeechError):
        onboard.configure(directory, "http")
    assert load(directory) == first
    monkeypatch.setattr("builtins.input", lambda _: "https://speech.example.org")
    onboard.configure(directory, "http")
    updated = load(directory)
    assert updated["devices"] == first["devices"]
    assert updated["link_credential"] == first["link_credential"]
    assert "DEEPGRAM_API_KEY" not in updated["speech"]
    assert updated["speech"]["WALKIEONE_SPEECH_TOKEN"]


def test_configure_refuses_piped_secrets(tmp_path):
    directory = tmp_path / "private"
    secret = secrets.token_urlsafe(32)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "personal-server/onboard.py"),
            "configure",
            "--directory",
            str(directory),
        ],
        input=secret,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 1
    assert "interactive terminal" in result.stderr
    assert secret not in result.stdout + result.stderr
    assert not directory.exists()


def test_configure_refuses_echo_fallback(tmp_path, monkeypatch):
    import warnings

    interactive(monkeypatch, "unused")

    def fallback(_):
        warnings.warn("cannot hide input", getpass.GetPassWarning)
        pytest.fail("Must stop before accepting echoed secret")

    monkeypatch.setattr(getpass, "getpass", fallback)
    with pytest.raises(getpass.GetPassWarning):
        onboard.configure(tmp_path / "private", "deepgram")
    assert not (tmp_path / "private").exists()


def test_configure_refuses_git_checkout_or_symlink(tmp_path, monkeypatch):
    interactive(monkeypatch, secrets.token_urlsafe(32))
    (tmp_path / ".git").mkdir()
    with pytest.raises(onboard.SetupError, match="outside every Git"):
        onboard.configure(tmp_path / "private", "deepgram")
    (tmp_path / "linked").symlink_to(tmp_path / "private")
    with pytest.raises(onboard.SetupError, match="symbolic link"):
        onboard.configure(tmp_path / "linked", "deepgram")


def test_provider_requests_require_approval_and_do_not_print_provider_errors(
    monkeypatch, capsys
):
    called = []
    fixture = RehearsalSpeech()

    def factory(config):
        called.append(config)
        return fixture

    monkeypatch.setattr(onboard, "speech_from_config", factory)
    with pytest.raises(onboard.SetupError, match="approval"):
        onboard.check_speech({}, False)
    assert not called
    onboard.check_speech({}, True)
    assert fixture.heard == [b"\0\0" * 2400]
    assert fixture.spoken == ["Your Walkie One server is ready."]
    secret = secrets.token_urlsafe(32)

    def fail(_):
        raise SpeechError(secret)

    fixture.transcribe = fail
    with pytest.raises(onboard.SetupError) as error:
        onboard.check_speech({}, True)
    assert secret not in str(error.value) + capsys.readouterr().out


def test_network_probe_real_tls_and_authentication(tmp_path):
    async def exercise():
        config = initialize(tmp_path / "private", "http://127.0.0.1:8787")
        with pytest.raises(onboard.SetupError, match="set-origin"):
            await onboard.check_network(config)
        _, trust = tls_contexts(tmp_path)
        async with running_server(
            create_app(config, RehearsalSpeech()),
            ssl_certfile=str(tmp_path / "relay-cert.pem"),
            ssl_keyfile=str(tmp_path / "relay-key.pem"),
        ) as port:
            config["origin"] = f"https://localhost:{port}"
            with pytest.raises(OSError):
                await onboard.check_network(config)
            presence = await onboard.check_network(config, ssl_context=trust)
            assert presence["cloud"] is True
            assert presence["link"] is False

    asyncio.run(exercise())


def test_network_probe_does_not_send_audio_or_execute_assistant(tmp_path):
    async def exercise():
        config = initialize(tmp_path / "private", "https://unused.example.org")
        speech = RehearsalSpeech()
        async with running_server(create_app(config, speech)) as port:
            await onboard.check_network(config, local_port=port)
            assert speech.heard == speech.spoken == []

    asyncio.run(exercise())


@pytest.mark.parametrize("behavior", ["accept", "redirect"])
def test_network_probe_refuses_unauthenticated_access_and_redirect(tmp_path, behavior):
    async def exercise():
        config = initialize(tmp_path / "private", "https://unused.example.org")
        app = FastAPI()

        @app.websocket("/device")
        async def endpoint(ws: WebSocket):
            if behavior == "redirect":
                await ws.send_denial_response(
                    RedirectResponse("https://example.org/login")
                )
            else:
                await ws.accept()
                await ws.close()

        async with running_server(app) as port:
            with pytest.raises(onboard.SetupError, match="unauthenticated|redirect"):
                await onboard.check_network(config, local_port=port)

    asyncio.run(exercise())


def test_network_cli_failure_withholds_credentials(tmp_path):
    config = initialize(tmp_path / "private", "http://127.0.0.1:8787")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "personal-server/onboard.py"),
            "check-network",
            "--directory",
            str(tmp_path / "private"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "set-origin" in result.stderr
    assert config["link_credential"] not in result.stdout + result.stderr
    assert next(iter(config["devices"].values())) not in result.stdout + result.stderr
