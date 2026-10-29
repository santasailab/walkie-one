"""Local configuration and scoped client profiles; no company authority."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import uuid
from pathlib import Path
from urllib.parse import urlsplit

MAX_DEVICES = 16
MAX_CONFIG_BYTES = 64 * 1024


def origin_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        and not (parsed.scheme == "http" and parsed.hostname == "127.0.0.1")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.port == 0
        or "\\" in value
        or any(ord(c) < 33 or ord(c) > 126 for c in value)
    ):
        raise ValueError("Use an HTTPS origin, or http://127.0.0.1:8787 locally")
    return value.rstrip("/")


def private_json(path: Path, value: dict, *, replace=False) -> None:
    """Write atomically without following a destination symlink."""
    temporary = path.with_name(path.name + "." + secrets.token_hex(8))
    try:
        with os.fdopen(
            os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as output:
            json.dump(value, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load(directory: Path) -> dict:
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ValueError("Configuration directory must be private (mode 0700)")
    with os.fdopen(
        os.open(directory / "server.json", os.O_RDONLY | os.O_NOFOLLOW), "rb"
    ) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("server.json must be a private regular file (mode 0600)")
        raw = source.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("Configuration is too large")
    config = json.loads(raw)
    validate(config)
    return config


def validate(config: dict) -> None:
    if (
        not isinstance(config, dict)
        or set(config)
        != {"version", "origin", "link_credential", "devices", "speech", "transcripts"}
        or type(config["version"]) is not int
        or config["version"] != 1
    ):
        raise ValueError("Invalid personal server configuration")
    origin_url(config["origin"])
    if not re.fullmatch(
        r"v2\.[0-9a-f]{32}\.[0-9a-f]{32}\.link\.[A-Za-z0-9_-]{43}",
        config["link_credential"],
    ):
        raise ValueError("Invalid Link credential")
    devices = config["devices"]
    if not isinstance(devices, dict) or not 1 <= len(devices) <= MAX_DEVICES:
        raise ValueError("Configure between one and sixteen devices")
    for name, credential in devices.items():
        if (
            name in {"link", "server"}
            or not re.fullmatch(r"[a-z0-9-]{1,32}", name)
            or not re.fullmatch(
                rf"v1\.{re.escape(name)}\.device\.[A-Za-z0-9_-]{{43}}", credential
            )
        ):
            raise ValueError("Invalid device name or credential")
    if (
        not isinstance(config["speech"], dict)
        or type(config["transcripts"]) is not bool
    ):
        raise ValueError("Invalid speech or transcript settings")
    allowed = {
        "WALKIEONE_STT_PROVIDER",
        "WALKIEONE_TTS_PROVIDER",
        "DEEPGRAM_API_KEY",
        "DEEPGRAM_STT_MODEL",
        "DEEPGRAM_TTS_MODEL",
        "WALKIEONE_SPEECH_URL",
        "WALKIEONE_SPEECH_TOKEN",
    }
    if set(config["speech"]) - allowed or any(
        not isinstance(value, str) for value in config["speech"].values()
    ):
        raise ValueError("Unknown speech setting")
    for key in ("WALKIEONE_STT_PROVIDER", "WALKIEONE_TTS_PROVIDER"):
        if config["speech"].get(key) not in {"deepgram", "http"}:
            raise ValueError("Choose deepgram or http for each speech provider")


def profiles(directory: Path, config: dict) -> None:
    base = config["origin"].replace("https://", "wss://").replace("http://", "ws://")
    private_json(
        directory / "link.json",
        {"url": base + "/link", "credential": config["link_credential"]},
        replace=True,
    )
    for name, credential in config["devices"].items():
        private_json(
            directory / f"{name}.json",
            {"url": base + "/device", "credential": credential},
            replace=True,
        )


def initialize(directory: Path, origin: str) -> dict:
    origin = origin_url(origin)
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    config = {
        "version": 1,
        "origin": origin,
        "link_credential": f"v2.{uuid.uuid4().hex}.{uuid.uuid4().hex}.link."
        + secrets.token_urlsafe(32),
        "devices": {"walkie-1": "v1.walkie-1.device." + secrets.token_urlsafe(32)},
        "speech": {
            "WALKIEONE_STT_PROVIDER": "deepgram",
            "WALKIEONE_TTS_PROVIDER": "deepgram",
            "DEEPGRAM_API_KEY": "",
        },
        "transcripts": False,
    }
    private_json(directory / "server.json", config)
    profiles(directory, config)
    return config
