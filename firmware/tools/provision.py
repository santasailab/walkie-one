"""Write one relay device credential, and optionally the mobile data settings,
to an ESP32 over its USB serial port."""

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
# The firmware's own rule (cellular_policy.h); nothing else reaches the modem.
APN_RE = re.compile(r"[A-Za-z0-9.-]{1,100}")
# Each console command, and the reply that confirms it. The radio is stopped
# before its profile changes and started only after the profile is accepted.
# The firmware queues each change and writes it afterwards, so the last step
# sends nothing and waits for the started settings to reach storage.
CELL_STEPS = (
    ("CELL STOP", "[cell] radio supply off"),
    ("CELL APN {apn}", "[cell] APN save queued"),
    ("CELL START", "[cell] start/save queued"),
    (None, "[cell] settings saved; auto-start=on"),
)
CELL_REFUSALS = (
    "[cell] invalid APN",
    "[cell] set CELL APN",
    "[cell] finish Talk",
    "[cell] unknown command",
    "[cell] command too long",
    "[cell] cellular worker not initialized",
    "[cell] configuration could not be saved",
)


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


def validate_apn(apn: str) -> str:
    if not APN_RE.fullmatch(apn):
        raise ValueError("APN must be 1-100 letters, digits, dots or hyphens")
    return apn


def credential_input(args: argparse.Namespace) -> str:
    if args.credential_file:
        return Path(args.credential_file).read_text(encoding="utf-8")
    if args.credential_stdin:
        return sys.stdin.read()
    return getpass.getpass("Device credential (input hidden): ")


def await_reply(board, deadline: float, accepted: str, refused: tuple, failure: str):
    """Read device lines until one starts with `accepted`; never echo them."""
    while time.monotonic() < deadline:
        line = board.readline().decode("utf-8", errors="replace").strip()
        if line.startswith(accepted):
            return line
        if line.startswith(refused):
            raise RuntimeError(failure)
    raise TimeoutError("device did not confirm; check port and firmware")


def provision(
    port: str,
    credential: str | None,
    pair_id: str | None,
    timeout: float,
    apn: str | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    with serial.Serial(port, 115200, timeout=0.2, write_timeout=2) as board:
        # Opening an ESP32 serial port can reset it. Give setup() time to finish,
        # then discard boot logs so success must come from this command.
        time.sleep(2.5)
        board.reset_input_buffer()
        if credential is not None:
            board.write(("PROVISION " + credential + "\n").encode("ascii"))
            board.flush()
            line = await_reply(
                board,
                deadline,
                "[provision] OK pair=",
                ("[provision] ERROR",),
                "device rejected provisioning",
            )
            if line.removeprefix("[provision] OK pair=") != pair_id:
                raise RuntimeError("device reported a different pair ID")
            print("Device credential provisioned successfully.")
        if apn is not None:
            for command, confirmation in CELL_STEPS:
                if command is not None:
                    board.write((command.format(apn=apn) + "\n").encode("ascii"))
                    board.flush()
                await_reply(
                    board,
                    deadline,
                    confirmation,
                    CELL_REFUSALS,
                    "device refused the mobile data settings",
                )
            print("Cellular settings stored; the modem is starting.")


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
    parser.add_argument(
        "--apn",
        help="also store this mobile data APN and start the modem (PCB06)",
    )
    parser.add_argument(
        "--keep-credential",
        action="store_true",
        help="with --apn: change only the mobile data settings",
    )
    parser.add_argument("--timeout", type=float, default=12.0)
    args = parser.parse_args()
    if args.keep_credential and not args.apn:
        parser.error("--keep-credential needs --apn")
    if args.keep_credential and (args.credential_file or args.credential_stdin):
        parser.error("--keep-credential takes no credential")

    try:
        apn = validate_apn(args.apn) if args.apn else None
        credential = pair_id = None
        if not args.keep_credential:
            credential, pair_id = extract_credential(credential_input(args))
        provision(args.port, credential, pair_id, args.timeout, apn)
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
