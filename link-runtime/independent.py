"""Launch the existing Link using only an owner's scoped Link credential."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit


def link_environment(
    path: Path, agent: str, *, development: bool = False, command_file=None
) -> dict:
    with os.fdopen(
        os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb"
    ) as source:
        if (
            not stat.S_ISREG(os.fstat(source.fileno()).st_mode)
            or os.fstat(source.fileno()).st_mode & 0o077
        ):
            raise ValueError("Link profile must be a regular mode-0600 file")
        raw = source.read(4097)
    if len(raw) > 4096:
        raise ValueError("Link profile is too large")
    profile = json.loads(raw)
    if not isinstance(profile, dict) or set(profile) != {"url", "credential"}:
        raise ValueError("Link profile must contain only url and credential")
    url, credential = profile["url"], profile["credential"]
    if not isinstance(url, str) or not isinstance(credential, str):
        raise TypeError("Link profile values must be text")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "wss"
        and not (
            development and parsed.scheme == "ws" and parsed.hostname == "127.0.0.1"
        )
        or not parsed.hostname
        or parsed.path != "/link"
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or "\\" in url
        or any(ord(c) < 33 or ord(c) > 126 for c in url)
        or parsed.port == 0
    ):
        raise ValueError(
            "Use a WSS /link URL; plaintext is restricted to explicit loopback development"
        )
    if not re.fullmatch(
        r"v2\.[A-Za-z0-9-]{1,64}\.[A-Za-z0-9-]{1,64}\.link\.[A-Za-z0-9_-]{43}",
        credential,
    ):
        raise ValueError("Profile must carry a scoped v2 Link credential")
    if agent not in {"codex", "claude", "custom"}:
        raise ValueError("Select codex or claude; custom adapters use AgentEndpoint")
    environment = dict(os.environ)
    if agent == "custom":
        from custom_endpoint import load_command

        if command_file is None:
            raise ValueError("A private local command file is required")
        load_command(command_file)
        environment["WALKIEONE_CUSTOM_COMMAND"] = str(Path(command_file).resolve())
    state = (
        Path.home() / ".relay" / "servers" / hashlib.sha256(url.encode()).hexdigest()
    )
    environment.update(
        RELAY_CLOUD_URL=url,
        RELAY_TOKEN=credential,
        RELAY_AGENT=agent,
        LINK_HOST="127.0.0.1",
        LINK_PORT="0",
        LINK_AUTH=secrets.token_urlsafe(32),
        WALKIEONE_STATE_DIRECTORY=str(state),
        WALKIEONE_WORKSPACE=str(state / "agent"),
        WALKIEONE_OUTBOX=str(state / "agent" / "outbox"),
        WALKIEONE_TASKS=str(state / "tasks"),
    )
    return environment


def main():
    parser = argparse.ArgumentParser(
        description="Connect Link to your own server without a Walkie One account"
    )
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--agent", choices=("codex", "claude", "custom"), required=True)
    parser.add_argument("--command-file", type=Path)
    parser.add_argument("--development", action="store_true")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the profile without connecting or starting an assistant",
    )
    args = parser.parse_args()
    try:
        environment = link_environment(
            args.profile,
            args.agent,
            development=args.development,
            command_file=args.command_file,
        )
    except (OSError, ValueError, TypeError):
        parser.exit(1, "Invalid Link profile or permissions; no credentials printed.\n")
    if args.check:
        print("Link profile is valid. No network or assistant process started.")
        return
    os.execve(
        sys.executable,
        [sys.executable, str(Path(__file__).with_name("link.py"))],
        environment,
    )


if __name__ == "__main__":
    main()
