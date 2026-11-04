"""Build the PCB06 application and recovery artifacts; never access a device."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import shutil
import struct
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE_VERSION = "3.3.11"
FQBN = (
    "esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi,FlashMode=qio,"
    "PartitionScheme=app3M_fat9M_16MB,CDCOnBoot=cdc,USBMode=hwcdc"
)
FLAGS = "-DRELAY_CELLULAR=1 -DRELAY_BOARD_06=1"
APP_LIMIT = 0x400000
KEY_PLACEHOLDER = "REPLACE_WITH_RELEASE_PUBLIC_KEY"
VERSION = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}")
PARTITIONS = """# Name,Type,SubType,Offset,Size,Flags
nvs,data,nvs,0x9000,0x5000,
otadata,data,ota,0xE000,0x2000,
factory,app,factory,0x10000,0x400000,
ota_0,app,ota_0,0x410000,0x400000,
ota_1,app,ota_1,0x810000,0x400000,
coredump,data,coredump,0xFF0000,0x10000,
"""
EXPECTED_PARTITIONS = [
    (1, 2, 0x9000, 0x5000, "nvs"),
    (1, 0, 0xE000, 0x2000, "otadata"),
    (0, 0, 0x10000, APP_LIMIT, "factory"),
    (0, 0x10, 0x410000, APP_LIMIT, "ota_0"),
    (0, 0x11, 0x810000, APP_LIMIT, "ota_1"),
    (1, 3, 0xFF0000, 0x10000, "coredump"),
]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def artifact(path: Path, run: Path, offset: int) -> dict:
    return {
        "path": str(path.relative_to(run)),
        "offset": hex(offset),
        "size": path.stat().st_size,
        "sha256": digest(path),
    }


def verify_partitions(path: Path) -> None:
    actual = []
    raw = path.read_bytes()
    for offset in range(0, len(raw), 32):
        entry = raw[offset : offset + 32]
        if len(entry) < 32 or entry[:2] != b"\xaa\x50":
            break  # MD5 trailer or erased padding follows the entries.
        _, kind, subtype, address, size, label, flags = struct.unpack(
            "<HBBII16sI", entry
        )
        if flags:
            raise ValueError("Unexpected partition flags")
        actual.append((kind, subtype, address, size, label.rstrip(b"\0").decode()))
    if actual != EXPECTED_PARTITIONS:
        raise ValueError(f"Generated partition table does not match PCB06: {actual!r}")


def release_key() -> str:
    """The published Ed25519 public key, or "" while the placeholder stands."""
    text = (ROOT / "firmware" / "release-key.pub").read_text().strip()
    if text == KEY_PLACEHOLDER:
        return ""
    try:
        key = base64.b64decode(text, validate=True)
    except binascii.Error as exc:
        raise ValueError("release-key.pub is not valid base64") from exc
    if len(key) != 32:
        raise ValueError("release-key.pub must hold a 32-byte Ed25519 public key")
    return text


def build_config(official: bool, version: str | None) -> str:
    """config.ci.h plus what makes an image official. Only the release
    workflow passes --official; such an image takes automatic updates."""
    config = (ROOT / "firmware" / "relay_client" / "config.ci.h").read_text()
    if version is not None:
        if not VERSION.fullmatch(version):
            raise ValueError("--version must be major.minor.patch, each 0-9999")
        config += f'\n#define RELAY_FW_SEMVER "{version}"\n'
    if official:
        if version is None:
            raise ValueError("--official needs --version")
        if not release_key():
            raise ValueError("--official needs the published key in release-key.pub")
        config += "#define RELAY_OFFICIAL_BUILD 1\n"
    return config


def stage_sources(sketch: Path, config: str = "") -> dict:
    sketch.mkdir()
    original = ROOT / "firmware" / "relay_client"
    # Root .cpp files include Wi-Fi/BLE pairing implementations. The cellular
    # target does not use those; its selected implementation is copied below.
    for source in original.glob("*.h"):
        if source.name != "config.h":
            shutil.copy2(source, sketch / source.name)
    shutil.copy2(original / "relay_client.ino", sketch / "relay_client.ino")
    shutil.copytree(original / "src", sketch / "src")
    cellular = ROOT / "firmware" / "cellular" / "main"
    for source in cellular.glob("*.h"):
        shutil.copy2(source, sketch / source.name)
    shutil.copy2(cellular / "cellular.cpp", sketch / "cellular.cpp")
    (sketch / "config.h").write_text(config or (original / "config.ci.h").read_text())
    (sketch / "release_key.h").write_text(
        "#pragma once\n// Generated from firmware/release-key.pub; empty accepts no update.\n"
        f'#define RELAY_RELEASE_KEY "{release_key()}"\n'
    )
    (sketch / "partitions.csv").write_text(PARTITIONS)
    return {
        str(path.relative_to(sketch)): digest(path)
        for path in sorted(sketch.rglob("*"))
        if path.is_file()
    }


def build(args: argparse.Namespace) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run = (args.output or ROOT / ".context" / "pcb06" / stamp).resolve()
    run.mkdir(parents=True, exist_ok=False)
    sketch = run / "relay_client"
    build_dir = run / "build"
    manifest = {
        "kind": "PCB06 application development build; hardware qualification required",
        "board": "06 / ESP32-S3-WROOM-1U N16R8 / UART SIM7672G",
        "fqbn": FQBN,
        "flags": FLAGS,
        "arduino_core_required": CORE_VERSION,
        "status": "building",
        "source_commit": subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
        ).strip(),
        "official": args.official,
        "version": args.version,
        "source_hashes": stage_sources(
            sketch, build_config(args.official, args.version)
        ),
        "config_source": "firmware/relay_client/config.ci.h; credentials are not embedded",
    }
    manifest_path = run / "manifest.json"

    def save_manifest():
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    save_manifest()
    print(f"Build output: {run}", flush=True)
    try:
        manifest["arduino_cli_version"] = subprocess.check_output(
            [args.arduino_cli, "version"], text=True
        ).strip()
        cores = subprocess.check_output([args.arduino_cli, "core", "list"], text=True)
        if not any(
            line.split()[:2] == ["esp32:esp32", CORE_VERSION]
            for line in cores.splitlines()
        ):
            raise RuntimeError(f"Arduino ESP32 core {CORE_VERSION} is required")
        manifest["esptool_version"] = subprocess.check_output(
            [sys.executable, "-m", "esptool", "version"], text=True
        ).strip()
        command = [
            args.arduino_cli,
            "compile",
            "--fqbn",
            FQBN,
            "--build-path",
            str(build_dir),
            "--build-property",
            "compiler.cpp.extra_flags=" + FLAGS,
            "--build-property",
            "compiler.c.extra_flags=" + FLAGS,
            "--build-property",
            f"upload.maximum_size={APP_LIMIT}",
            str(sketch),
        ]
        manifest["compile_command"] = command
        save_manifest()
        log_path = run / "build.log"
        with log_path.open("w") as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)

        bootloader = build_dir / "relay_client.ino.bootloader.bin"
        partitions = build_dir / "relay_client.ino.partitions.bin"
        application = build_dir / "relay_client.ino.bin"
        header = bootloader.read_bytes()[:4]
        # Arduino's QIO menu option intentionally emits a DIO image header.
        # Preserve that actual header; rewriting it to QIO can prevent boot.
        if len(header) != 4 or header[0] != 0xE9 or header[2] != 2:
            raise ValueError(
                f"Expected Arduino DIO bootloader header, got {header.hex()}"
            )
        if bootloader.stat().st_size > 0x8000:
            raise ValueError("Bootloader overlaps the partition table")
        verify_partitions(partitions)
        if partitions.stat().st_size > 0x1000:
            raise ValueError("Partition table overlaps NVS")
        if not 0 < application.stat().st_size <= APP_LIMIT:
            raise ValueError("Application does not fit the 4 MiB factory slot")

        # Arduino's boot_app0.bin selects ota_0, which is not where this build
        # puts the application. Erased otadata selects the factory partition.
        otadata = run / "otadata-factory.bin"
        otadata.write_bytes(b"\xff" * 0x2000)
        components = [
            (0, bootloader),
            (0x8000, partitions),
            (0xE000, otadata),
            (0x10000, application),
        ]
        manifest["components"] = [artifact(path, run, at) for at, path in components]
        manifest["bootloader_header_hex"] = header.hex()
        manifest["preserved_nvs"] = {"offset": "0x9000", "size": "0x5000"}
        merged = run / "WalkieOne06_Application.compact.bin"
        merge_command = [
            sys.executable,
            "-m",
            "esptool",
            "--chip",
            "esp32s3",
            "merge_bin",
            "-o",
            str(merged),
            "--flash_mode",
            "keep",
            "--flash_freq",
            "keep",
            "--flash_size",
            "keep",
        ]
        for address, path in components:
            merge_command.extend((hex(address), str(path)))
        manifest["merge_command"] = merge_command
        with (run / "merge.log").open("w") as log:
            subprocess.run(
                merge_command, stdout=log, stderr=subprocess.STDOUT, check=True
            )
        merged_bytes = merged.read_bytes()
        for address, path in components:
            if (
                merged_bytes[address : address + path.stat().st_size]
                != path.read_bytes()
            ):
                raise ValueError(f"Merge changed component bytes: {path.name}")
        if len(merged_bytes) != 0x10000 + application.stat().st_size:
            raise ValueError("Compact image has unexpected trailing padding")
        manifest["compact_merged"] = artifact(merged, run, 0)
        manifest["compact_merged_preserves_nvs"] = False
        manifest["flash_note"] = (
            "No device was accessed. Component ranges exclude NVS. The compact merged "
            "image includes erased padding over NVS and would erase saved credentials. "
            "Use the listed components for settings-preserving flashing; ignore the "
            "Arduino-generated padded merged image and boot_app0.bin."
        )
        manifest["status"] = "built; not flashed or hardware qualified"
        save_manifest()
        print(f"Manifest: {manifest_path}")
        print(f"Application: {application.stat().st_size} / {APP_LIMIT} bytes")
        return manifest_path
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = str(exc)
        save_manifest()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arduino-cli", default="arduino-cli")
    parser.add_argument(
        "--output", type=Path, help="New output directory; must not exist"
    )
    parser.add_argument("--version", help="Firmware version, major.minor.patch")
    parser.add_argument(
        "--official",
        action="store_true",
        help="Release workflow only: an image that takes automatic updates",
    )
    args = parser.parse_args()
    try:
        build(args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
