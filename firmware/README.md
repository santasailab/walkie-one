# Walkie One firmware

This source runs the Walkie One **PCB06** handheld: ESP32-S3-WROOM-1U N16R8,
SIM7672G on UART, SPH0690 PDM microphone and MAX98357A speaker amplifier.
It includes the current application, a reproducible build tool, USB credential
provisioning, signed-image verification and host tests. No credentials or
precompiled device images are included.

Talk records up to 60 seconds of 24 kHz mono PCM; the server returns a complete
MP3 reply that plays when you press Listen. The application includes the current
button sounds, microphone gain/crop, charging lights, battery reports, cellular
startup and reconnect handling. Custom builds disable automatic updates.

This is a **source release**, not a new qualified hardware or binary release.
The software checks and target build do not prove your board's wiring, SIM,
antenna, battery, audio or recovery path. Older 8 MB boards and USB-modem
prototypes have different pin assignments; do not flash a PCB06 build onto them.
Legacy helpers remain in source for compatibility, but PCB06 is the documented target.

## Build your own firmware

Use Python 3.12, a C++ compiler and Arduino CLI. Install the exact Arduino ESP32
core **3.3.11** using its official board index:

```sh
arduino-cli core update-index --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core install esp32:esp32@3.3.11 --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
./scripts/setup.sh
.venv/bin/python firmware/tools/build_pcb06.py
```

The tool never opens USB or flashes a device. It stages only build inputs,
checks the core version and partition table, and writes component hashes and
offsets to `.context/pcb06/<timestamp>/manifest.json`. The source hash inventory
is separate from the per-build manifest.

For your personal server, first edit the **public hostname** in
`relay_client/config.ci.h`: replace `your-server.example` with your server's
hostname, without a scheme, port or path. Leave `/device` as the path. The
firmware connects with WSS on port 443. Never put a credential, Wi-Fi password,
provider key or private TLS key in that file. This placeholder cannot connect
to the company service by accident.

The included trust anchor is the public ISRG Root X2 certificate. Your server's
TLS chain must validate against it. If your server uses another CA, replace
`RELAY_CA` in `relay_client.ino` with the appropriate **public CA certificate**
and rebuild. Keep `tls.setCACert` and hostname verification enabled; do not use
`setInsecure`. Test your public WSS route before installing a device build.

Run `build_pcb06.py --help` for output and version options. Do not pass
`--official` for personal builds. That option and `release.py` exist for release
maintainers, require the corresponding private signing key outside Git, and do
not grant access to company services.

## Pins to leave alone

| Function | PCB06 GPIO |
| --- | --- |
| Native USB | 19 / 20 |
| USB service mux | 39 |
| Talk / Listen / volume up / volume down | 1 / 5 / 2 / 4 |
| RGB LED (active low) | 47 / 48 / 44 |
| Microphone clock / data | 6 / 7 |
| Speaker BCLK / LRC / data / enable | 10 / 11 / 40 / 13 |
| Modem TX / RX | 17 / 18 |
| Modem supply / PWRKEY / reset / USB boot / DTR | 21 / 14 / 15 / 42 / 16 |
| Battery gauge I2C | 8 / 9 |

Preserve USB and mux pins so service access remains possible. Check these
against your board before powering or writing it. Host tests check the mapping;
they cannot inspect your physical board.

## Install and provision

First run the [personal-server rehearsal](../personal-server/README.md), then
set up authenticated public WSS and your Link. A working server alone does not
qualify the device. Confirm the exact board and keep a private flash/NVS backup
before any write. NVS contains credentials and must never be uploaded to an issue.

The build manifest lists four separate writes: bootloader at `0x0`, partition
table at `0x8000`, factory OTA selection at `0xE000`, and application at `0x10000`.
Use the verified components and offsets from **your own manifest**. They leave
NVS (`0x9000`–`0xDFFF`) untouched. The merged image contains erased padding over
NVS; writing it as one range would erase saved credentials. Do not use
`erase_flash`, a merged image, or Arduino's default `boot_app0.bin` to preserve
an existing device. Changing the partition layout requires separate recovery review.

After installing your custom build on a confirmed PCB06, use the credential
created for this device by your personal server. Run this in your own Terminal;
it asks for the credential with hidden input:

```sh
.venv/bin/python firmware/tools/provision.py --port /dev/cu.YOUR_DEVICE
```

Do not give the tool the Link token. Credential validation checks its format;
your selected server decides whether it is authorized. A personal-server
credential grants no access to the company service. Close other serial clients
before provisioning. Opening serial may reset the board.

In a local 115200-baud serial terminal, while idle, use `CELL STOP`,
`CELL APN <your-provider-apn>`, then `CELL START`. The APN belongs to the SIM
issuer, which may differ from the roaming network. Never paste a SIM identifier
or a private serial/NVS dump into chat. Verify registration, PPP, DNS, TLS and
authenticated WebSocket separately; an AT response is not an internet connection.

## Restore official firmware

`tools/restore.py` verifies a release's size, SHA-256 and Ed25519 signature using
`release-key.pub` before writing the application. The public verification key
is safe to distribute; the corresponding private key is not included.
It leaves NVS intact and changes only OTA selection and the factory application.

This source snapshot does **not** include a public recovery download endpoint or
a qualified recovery bundle. The default URL deliberately cannot resolve. Obtain
a verified PCB06 bundle and establish USB recovery for your unit before changing
its firmware. With a separately supplied bundle, `restore.py --help` describes
its offline `--bundle` option. Do not invent a download URL or assume a factory
restore can repair an erased bootloader or partition table.

Custom builds ignore managed firmware offers. The personal server does not
provide managed updates. Restoring an official image does not rotate a
credential, transfer ownership, or provide company hosting.

## Check changes

```sh
./scripts/check.sh
./scripts/security.sh
.venv/bin/python firmware/tools/build_pcb06.py
```

The host suite covers capture/release under stalled transport, PCM saturation,
button gestures, LEDs, playback receipts, reconnects, modem startup, signed
restore verification and MP3 decoding with address/undefined-behavior sanitizers.
FFmpeg adds independent decode and resampling comparisons. The optional legacy
Muse pairing test needs mbedTLS 3 and cJSON; it is unrelated to the PCB06 target.

On hardware, separately check a full Talk/Listen exchange, two-device routing,
network loss, battery/charging transitions, quiet OTA restart, interrupted
playback and recovery. This release makes no new hardware-test claim.
