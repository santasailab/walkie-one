"""Write one relay device credential to an ESP32 over its USB serial port."""

from __future__ import annotations

import argparse
import getpass
import re
import sys
import time
from pathlib import Path

try:
    import serial
except ImportError:
    sys.exit("pyserial is required: run ./scripts/setup.sh from the repository root")


CREDENTIAL_RE = re.compile(r"^v1\.([a-z0-9-]{1,32})\.device\.([A-Za-z0-9_-]{43})$")


def extract_credential(raw: str) -> tuple[str, str]:
    """Accept a bare credential or the three-line output from relay.py --mint."""
    candidates = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("device "):
            candidates.append(line.split(None, 1)[1])
        elif line.startswith("v1."):
            candidates.append(line)
    if len(candidates) != 1:
        raise ValueError("input must contain exactly one device credential")
    match = CREDENTIAL_RE.fullmatch(candidates[0])
    if not match:
        raise ValueError("credential must be v1.<pair>.device.<signature>")
    return candidates[0], match.group(1)


def credential_input(args: argparse.Namespace) -> str:
    if args.credential_file:
        return Path(args.credential_file).read_text(encoding="utf-8")
    if args.credential_stdin:
        return sys.stdin.read()
    return getpass.getpass("Device credential (input hidden): ")


def provision(port: str, credential: str, pair_id: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    with serial.Serial(port, 115200, timeout=0.2, write_timeout=2) as board:
        # Opening an ESP32 serial port can reset it. Give setup() time to finish,
        # then discard boot logs so success must come from this command.
        time.sleep(2.5)
        board.reset_input_buffer()
        board.write(("PROVISION " + credential + "\n").encode("ascii"))
        board.flush()

        while time.monotonic() < deadline:
            line = board.readline().decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if line.startswith("[provision] OK pair="):
                reported = line.removeprefix("[provision] OK pair=")
                if reported != pair_id:
                    raise RuntimeError("device reported a different pair ID")
                print("Device credential provisioned successfully.")
                return
            if line.startswith("[provision] ERROR"):
                raise RuntimeError("device rejected provisioning")
    raise TimeoutError("device did not confirm provisioning; check port and firmware")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Store a relay device credential in ESP32 NVS over USB serial."
    )
    parser.add_argument(
        "--port", required=True, help="serial port, e.g. /dev/cu.usbmodem1101"
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--credential-file", help="file containing a credential or --mint output"
    )
    source.add_argument(
        "--credential-stdin",
        action="store_true",
        help="read a credential or --mint output from stdin",
    )
    parser.add_argument("--timeout", type=float, default=12.0)
    args = parser.parse_args()

    try:
        credential, pair_id = extract_credential(credential_input(args))
        provision(args.port, credential, pair_id, args.timeout)
    except (
        OSError,
        ValueError,
        RuntimeError,
        TimeoutError,
        serial.SerialException,
    ) as exc:
        parser.exit(1, f"provisioning failed: {exc}\n")


if __name__ == "__main__":
    main()
