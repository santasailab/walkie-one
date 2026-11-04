"""Sign a 06 firmware image and write its release manifest; never uploads.

`sign` reads the Ed25519 private key (base64 of the raw 32-byte seed) from the
FIRMWARE_SIGNING_KEY environment variable, signs the message restore.py and the
Device check, and refuses a key that does not match release-key.pub. `pause`
writes the manifest that withdraws a stage. The release workflow uploads both.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).parent))
from restore import (
    KEY_PATH,
    RestoreError,
    parse_public_key,
    signed_message,
)

BOARD = "06"
MAX_IMAGE_BYTES = 4 * 1024 * 1024
VERSION = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}")


class ReleaseError(Exception):
    pass


def private_key(text: str) -> Ed25519PrivateKey:
    try:
        seed = base64.b64decode(text.strip(), validate=True)
    except binascii.Error as exc:
        raise ReleaseError("FIRMWARE_SIGNING_KEY is not valid base64") from exc
    if len(seed) != 32:
        raise ReleaseError("FIRMWARE_SIGNING_KEY must be a 32-byte Ed25519 seed")
    return Ed25519PrivateKey.from_private_bytes(seed)


def sign(
    image: bytes, version: str, url: str, key_text: str, public_key: bytes
) -> dict:
    if not VERSION.fullmatch(version):
        raise ReleaseError("version must be major.minor.patch, each 0-9999")
    if not 0 < len(image) <= MAX_IMAGE_BYTES:
        raise ReleaseError("image must be 1 byte to 4 MiB")
    if not url.startswith("https://") or "?" in url or "#" in url:
        raise ReleaseError("image URL must be plain https")
    key = private_key(key_text)
    if key.public_key().public_bytes_raw() != public_key:
        raise ReleaseError("FIRMWARE_SIGNING_KEY does not match release-key.pub")
    sha256 = hashlib.sha256(image).hexdigest()
    signature = key.sign(signed_message(BOARD, version, sha256))
    return {
        "board": BOARD,
        "version": version,
        "url": url,
        "size": len(image),
        "sha256": sha256,
        "signature": base64.b64encode(signature).decode(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    signing = commands.add_parser("sign", help="sign an image and write its manifest")
    signing.add_argument("--image", type=Path, required=True)
    signing.add_argument("--version", required=True)
    signing.add_argument("--url", required=True, help="where Devices will download it")
    signing.add_argument("--out", type=Path, required=True)
    signing.add_argument("--public-key", type=Path, default=KEY_PATH)
    pausing = commands.add_parser(
        "pause", help="write a manifest that withdraws a stage"
    )
    pausing.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "pause":
            manifest = {"board": BOARD, "version": None}
        else:
            manifest = sign(
                args.image.read_bytes(),
                args.version,
                args.url,
                os.environ.get("FIRMWARE_SIGNING_KEY", ""),
                parse_public_key(args.public_key.read_text()),
            )
        args.out.write_text(json.dumps(manifest, indent=2) + "\n")
    except (OSError, ReleaseError, RestoreError) as exc:
        print(f"release failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({k: v for k, v in manifest.items() if k != "signature"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
