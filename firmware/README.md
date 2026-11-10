# Walkie One firmware

This source runs the Walkie One **PCB06** handheld: ESP32-S3-WROOM-1U N16R8,
SIM7672G on UART, SPH0690 PDM microphone and MAX98357A speaker amplifier.
It includes the current application, a reproducible build tool, USB credential
provisioning, signed-image verification and host tests. No credentials or
precompiled device images are included.

Talk records up to 60 seconds of 24 kHz mono PCM; the server returns a complete
MP3 reply that plays when you press Listen. Version `0.9.9-pcb06` includes the current
button sounds, 8x microphone gain and opening crop, steady orange charging
light, battery reports, cellular startup and reconnect handling. The held
Talk interval is silent, with the amplifier off; opening and closing cues
remain audible. Custom builds disable automatic updates.

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

The included trust anchors are the public ISRG Root X2 and ISRG Root X1
certificates. Your server's TLS chain must validate against one of them. If
your server uses another CA, replace `RELAY_CA` in `relay_client.ino` with the
appropriate **public CA certificate** and rebuild. Keep `tls.setCACert` and hostname verification enabled; do not use
`setInsecure`. Test your public WSS route before installing a device build.

Certificate validation needs a clock. The firmware can seed its clock from a
plausible modem `AT+CCLK?` reading, lets SNTP correct it when reachable,
and falls back to the image's build date. Automatic modem time updates stay
disabled (`AT+CTZU=0`): enabling them caused repeated internal flash-write
crashes on an inspected SIM7672G B05. Startup reads that persistent setting,
changes it only when needed and verifies the result before enabling the radio.
A modem with no valid clock leaves the ESP32 dependent on SNTP or the build
fallback. The SIM7672G reports its time in
UTC with the network's zone appended; the firmware uses the digits as UTC and
ignores the zone, so a modem that reports local time instead would be off by
that zone until SNTP answers. A clock more than an hour away from the
network's time in either direction is replaced by it before each dial until
SNTP has answered, so a wrong earlier seed does not outlive the soft-off
sleep that keeps the clock running. That fallback stops working once the
build is older than your server's current certificate, so do not rely on it:
check that SNTP is reachable or the modem already has valid time. The firmware
never enables automatic modem time updates to obtain it.

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

## USB-JTAG flashing

PCB06 can expose USB serial while serial bootloader flashing still fails.
Use the ESP32-S3 native **USB-JTAG** path with the verified Espressif OpenOCD
version in [the flashing guide](FLASHING.md). It explains boot-loop halting,
exact component offsets, private NVS backup/comparison and post-boot checks.
`restore.py` is serial-only and does not automatically switch to JTAG.

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

Add `--apn <your-provider-apn>` to store the mobile data APN and start the
modem in the same run, or `--apn <value> --keep-credential` to change only
those settings. The tool sends the same console commands you can type in a
local 115200-baud serial terminal while idle: `CELL STOP`,
`CELL APN <your-provider-apn>`, then `CELL START`. The APN belongs to the SIM
issuer, which may differ from the roaming network. Never paste a SIM identifier
or a private serial/NVS dump into chat. Verify registration, PPP, DNS, TLS and
authenticated WebSocket separately; an AT response is not an internet connection.

The modem's supply is never cut while it runs. Before `CELL STOP`, a
settings change, a restart of the data session, switching off or any other
firmware restart, the worker ends the session, holds PWRKEY for the module's
power-off gesture (2.6 s) and waits 3 s for it to shut down, then removes
the supply. A restart waits up to 10 s for that to finish, so switching off
takes a few seconds longer than the sound suggests. The 3 s settle time is a
placeholder: PCB06 has no STATUS line, so it awaits a measurement on hardware.

Once started, the modem is retried without an attempt limit. A failed attempt
powers the radio down, then waits 5, 15 and 30 seconds, doubling up to five
minutes between later attempts. A data session held for 30 seconds starts the
sequence again. `CELL STOP` is the only thing that ends the retries.

A data session can report an address while nothing crosses it. When dials
never reach your server, or an open connection goes silent, at least three
times over three minutes while the modem reports data, the firmware
power-cycles the modem and dials again. Each restart that does not help
doubles that period, up to an hour, so a server outage does not keep cycling
the radio. Any answer from the server resets it, including a refusal or a
certificate the Device rejects: neither is the modem's fault.

The server connection allows 15 seconds for the TCP connect and for a write
that stops making progress, then closes and redials. An upload that keeps
moving is not cut off, unless a single frame takes more than 30 seconds. The
server has 20 seconds to finish its WebSocket upgrade response.

A start on an empty battery without USB power (3 % or less, or under 3.60 V at
rest) does not power the modem. The Device stays switched off, as it does at
the same level while running, until it is charged and switched on again. An
unreadable gauge does not block a start. The voltage floor is still a
placeholder awaiting a measured brownout point on real hardware.

The gauge can read healthy while the battery sags under the modem. Three
brownout restarts in a row, with less than five minutes of running between
them, also end switched off. Switching on again is a fresh attempt.

The volume you choose is saved three seconds after the last change and
restored at the next start, including after switching off and on. It is
never written during Talk or playback.

The serial log prints why each boot happened (`[boot] cause=...`), and the
3-second `[stat]` line includes the lowest free heap since boot. A restart
after a crash, watchdog or brownout comes back without the boot chirp.

The Talk opening sound means the microphone is open. A press made while the
server is unreachable, or while the previous request is still uploading,
cannot be recorded and stays silent from press to release.

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

Continuous integration runs the same three commands' public parts: the host
suite, the security scan, and the PCB06 target build with the placeholder
hostname (Arduino CLI 1.5.1, core 3.3.11, checksummed download, no cache). The target
build catches what the host suite cannot, such as an auto-generated prototype
placed before its type's include; it proves nothing about hardware.

The host suite covers capture/release under stalled transport, PCM saturation,
button gestures, LEDs, playback receipts, reconnects, modem startup, signed
restore verification and MP3 decoding with address/undefined-behavior sanitizers.
FFmpeg adds independent decode and resampling comparisons. The optional legacy
Muse pairing test needs mbedTLS 3 and cJSON; it is unrelated to the PCB06 target.

On hardware, separately check a full Talk/Listen exchange, two-device routing,
network loss, battery/charging transitions, quiet OTA restart, interrupted
playback and recovery. This release makes no new hardware-test claim.
