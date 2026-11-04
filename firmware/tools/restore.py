"""Restore official WalkieOne firmware over USB without touching saved settings.

Downloads the latest approved release for a board, checks its size, SHA-256 and
Ed25519 signature against firmware/release-key.pub, erases `otadata` and writes
the image into the `factory` slot. NVS (0x9000, which holds the Device
credential) is never erased or written, and the whole flash is never erased.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import subprocess
import sys
import tempfile
import re
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# No public release base exists yet; set this when the Blob container is live.
MANIFEST_BASE = "https://releases.invalid/walkieone-firmware"
MANIFEST_URL = "{base}/{board}/everyone.json"
KEY_PLACEHOLDER = "REPLACE_WITH_RELEASE_PUBLIC_KEY"
KEY_PATH = Path(__file__).parents[1] / "release-key.pub"
OTADATA = (0xE000, 0x2000)
FACTORY = 0x10000
MAX_IMAGE_BYTES = 4_194_304  # the 4 MiB factory slot
MAX_MANIFEST_BYTES = 64_000
REQUIRED = ("board", "version", "url", "size", "sha256", "signature")


def signed_message(board: str, version: str, sha256_hex: str) -> bytes:
    """The bytes the release key signs: the image's identity, not its digest."""
    return f"walkieone-firmware\n{board}\n{version}\n{sha256_hex}\n".encode()


class RestoreError(Exception):
    pass


def https_url(url: str) -> str:
    if not isinstance(url, str):
        raise RestoreError("release URL must be text")
    try:
        parts = urllib.parse.urlsplit(url)
        valid = (
            parts.scheme == "https"
            and parts.hostname
            and parts.port != 0
            and parts.username is None
            and parts.password is None
            and not parts.query
            and not parts.fragment
            and "\\" not in url
            and all(32 < ord(c) < 127 for c in url)
        )
    except ValueError:
        valid = False
    if not valid:
        raise RestoreError("release URL must be plain HTTPS without credentials")
    return url


class HttpsRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, https_url(newurl))


def fetch(url: str, limit: int) -> bytes:
    opener = urllib.request.build_opener(HttpsRedirect())
    # Both the first URL and every redirect are constrained to HTTPS above.
    with opener.open(https_url(url), timeout=30) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise RestoreError(f"{url} is larger than {limit} bytes")
    return data


def load_manifest(raw: bytes, board: str) -> dict:
    try:
        manifest = json.loads(raw)
    except ValueError as exc:
        raise RestoreError("release manifest is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise RestoreError("release manifest must be a JSON object")
    if manifest.get("version") is None:  # a paused stage
        raise RestoreError(f"no approved release for board {board}")
    missing = [name for name in REQUIRED if name not in manifest]
    if missing:
        raise RestoreError("release manifest is missing " + ", ".join(missing))
    if manifest["board"] != board:
        raise RestoreError(f"release manifest is for board {manifest['board']}")
    https_url(manifest["url"])
    if not re.fullmatch(
        r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}", str(manifest["version"])
    ):
        raise RestoreError("release version must be major.minor.patch")
    if not isinstance(manifest["sha256"], str) or not re.fullmatch(
        r"[a-f0-9]{64}", manifest["sha256"]
    ):
        raise RestoreError("release SHA-256 must be 64 lowercase hex characters")
    size = manifest["size"]
    if not isinstance(size, int) or isinstance(size, bool):
        raise RestoreError("release size must be an integer")
    if not 0 < size <= MAX_IMAGE_BYTES:
        raise RestoreError(f"release size must be 1 to {MAX_IMAGE_BYTES} bytes")
    return manifest


def parse_public_key(text: str) -> bytes:
    text = text.strip()
    if text == KEY_PLACEHOLDER:
        raise RestoreError(
            "the release key is not published yet (firmware/release-key.pub "
            "holds a placeholder); refusing to flash"
        )
    try:
        key = base64.b64decode(text, validate=True)
    except binascii.Error as exc:
        raise RestoreError("release key is not valid base64") from exc
    if len(key) != 32:
        raise RestoreError("release key must be a 32-byte Ed25519 public key")
    return key


def verify_image(manifest: dict, image: bytes, public_key: bytes) -> None:
    if len(image) != manifest["size"]:
        raise RestoreError(
            f"image size is {len(image)} bytes, release says {manifest['size']}"
        )
    digest = hashlib.sha256(image)
    if digest.hexdigest() != manifest["sha256"]:
        raise RestoreError("image SHA-256 does not match the release")
    # Ed25519 hashes internally; the message binds board and version to the image.
    message = signed_message(manifest["board"], manifest["version"], digest.hexdigest())
    try:
        signature = base64.b64decode(manifest["signature"], validate=True)
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except (binascii.Error, ValueError, InvalidSignature) as exc:
        raise RestoreError("image signature is not valid for the release key") from exc


def esptool_argv(port: str, image_path: str) -> list[list[str]]:
    esptool = [sys.executable, "-m", "esptool", "--port", port]
    return [
        # The chip stays in download mode between the two commands.
        [*esptool, "--before", "usb_reset", "--after", "no_reset", "erase_region"]
        + [hex(OTADATA[0]), hex(OTADATA[1])],
        [
            *esptool,
            "--before",
            "no_reset",
            "--after",
            "hard_reset",
            "write_flash",
            hex(FACTORY),
            image_path,
        ],
    ]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Restore official firmware to a WalkieOne, keeping its settings."
    )
    parser.add_argument(
        "--port", required=True, help="serial port, e.g. /dev/cu.usbmodem1101"
    )
    parser.add_argument("--board", default="06")
    parser.add_argument(
        "--bundle",
        metavar="FILE",
        help="release manifest JSON already downloaded; the image file named at "
        "the end of its url must sit beside it. Skips the download.",
    )
    parser.add_argument(
        "--manifest-base",
        default=MANIFEST_BASE,
        help="base URL of the release manifests (default is a placeholder)",
    )
    args = parser.parse_args()

    try:
        public_key = parse_public_key(KEY_PATH.read_text(encoding="utf-8"))
        if args.bundle:
            bundle = Path(args.bundle)
            manifest = load_manifest(bundle.read_bytes(), args.board)
            name = urllib.parse.urlsplit(manifest["url"]).path.rsplit("/", 1)[-1]
            if name in {"", ".", ".."} or "%" in name:
                raise RestoreError("bundle image must name a file beside its manifest")
            path = bundle.parent / name
            if path.is_symlink() or not path.is_file():
                raise RestoreError("bundle image must be a regular file, not a symlink")
            with path.open("rb") as source:
                image = source.read(MAX_IMAGE_BYTES + 1)
        else:
            if args.manifest_base == MANIFEST_BASE:
                raise RestoreError(
                    "no public release location exists yet; pass --manifest-base "
                    "or --bundle"
                )
            url = MANIFEST_URL.format(base=args.manifest_base, board=args.board)
            manifest = load_manifest(fetch(url, MAX_MANIFEST_BYTES), args.board)
            image = fetch(manifest["url"], MAX_IMAGE_BYTES)
        verify_image(manifest, image, public_key)
        print(f"Verified firmware {manifest['version']} for board {args.board}")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "firmware.bin"
            path.write_bytes(image)  # flash the bytes that were verified
            for argv in esptool_argv(args.port, str(path)):
                subprocess.run(argv, check=True)
    except (OSError, RestoreError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"restore failed: {exc}\n")
    print("Restored. The Device keeps its credential and will update itself.")


if __name__ == "__main__":
    main()
