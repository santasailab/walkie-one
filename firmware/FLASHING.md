# Flash PCB06 through native USB-JTAG

**If serial flashing fails, use the ESP32-S3's built-in USB-JTAG interface with
Espressif OpenOCD.** A USB serial port appearing does not mean its ROM serial
bootloader is reachable. On tested PCB06 boards, serial flashing and repeated
BOOT/RESET attempts lost USB access, while USB-JTAG could halt the CPU and
program the flash. This is a board-specific recovery path, not evidence that
all ESP32-S3 chips are defective.

The same USB-C **data** cable carries this debugger connection; no external JTAG
adapter is needed when the board routes native USB to the connector. This guide
is for PCB06's ESP32-S3-WROOM-1U **N16R8**, not the cellular modem or older board
revisions. ESPressif documents the built-in interface and `program_esp` in its
[JTAG guide](https://docs.espressif.com/projects/esp-idf/en/release-v5.5/esp32s3/api-guides/jtag-debugging/index.html).

## Before connecting

- Confirm the board and requested operation. A connected cable is not permission
  for an AI to overwrite firmware. Retain a known-good image and recovery plan.
- Use a known-good data cable, close serial monitors and browser flashers, and
  accept the OS accessory prompt if shown. Follow your board's power procedure;
  the documented bench sessions used USB power with the battery disconnected.
- Preserve GPIO 19/20 (native USB) and GPIO 39 (service mux). Firmware that
  repurposes these can remove this access path. Do not burn eFuses, alter Secure
  Boot/flash encryption, or apply this procedure to an encrypted/locked board.
- Keep USB identities, OpenOCD logs and NVS backups private. NVS contains the
  device credential. Never upload a full flash dump to an issue or AI chat.

## Use the Espressif build of OpenOCD

The verified tool was **v0.12.0-esp32-20260703**, with the binary and scripts from
one installation. Generic upstream OpenOCD is not an interchangeable substitute.
Use the [Espressif release](https://github.com/espressif/openocd-esp32/releases/tag/v0.12.0-esp32-20260703)
for your platform, or the matching ESP-IDF installation. Verify the release
archive using its published checksum. This repository does not bundle it.

Set `OPENOCD_ROOT` to the installed directory containing both `bin/openocd` and
`share/openocd/scripts`, then check the version:

```sh
"$OPENOCD_ROOT/bin/openocd" --version
```

On macOS, inspect the actual native USB identity in System Information → USB,
or with `ioreg -a -r -c IOUSBHostDevice`. Expect VID:PID **303a:1001**, typically
named **USB JTAG_serial debug unit**. Select its observed USB serial, not a
`/dev/cu.*` pathname. Linux can inspect it with `lsusb -v -d 303a:1001` and Windows
with Device Manager. An AI running in a sandbox may need separately authorized
hardware access; an empty restricted listing does not prove the board is absent.

If it re-enumerates repeatedly, leave BOOT/RESET released and use the halt step
below. Do not keep cycling buttons or automatically select another connected
board. If native USB never appears, check cable, power, connector routing and OS
access first: JTAG cannot reach an interface that the host cannot see.

## Halt a boot loop

Save this as `halt.cfg` in a private working directory. Replace
`YOUR_USB_SERIAL` with the observed serial of the intended board:

```tcl
gdb port disabled
tcl port disabled
telnet port disabled
set ESP_RTOS none
source [find board/esp32s3-builtin.cfg]
adapter serial YOUR_USB_SERIAL
init
targets esp32s3.cpu0
halt 1500
if {[esp32s3.cpu0 curstate] ne "halted"} {error "CPU0 did not halt"}
echo "CPU0_HALT_CONFIRMED"
shutdown
```

```sh
"$OPENOCD_ROOT/bin/openocd" -s "$OPENOCD_ROOT/share/openocd/scripts" -f halt.cfg
```

Require exit status 0 and `CPU0_HALT_CONFIRMED`. Halting pauses execution and
changes volatile debugger/watchdog state; it does not write firmware or eFuses.
Leave the board connected afterward. An initial CPU1 examination failure was
observed on blank boards; confirmed CPU0 access is the relevant halt check.
Bound a halt attempt to about 20 seconds. Preserve an error and diagnose it
instead of retrying indefinitely.

## Write separate components and preserve NVS

First build with [the pinned source procedure](README.md#build-your-own-firmware).
Work in that build's `.context/pcb06/<timestamp>/` directory. Read `manifest.json`
and verify **each file's SHA-256, size and offset** before continuing. It must
contain this PCB06 layout:

| Component | Offset | File in the generated build |
| --- | --- | --- |
| Bootloader | `0x0` | `build/relay_client.ino.bootloader.bin` |
| Partition table | `0x8000` | `build/relay_client.ino.partitions.bin` |
| Factory selection | `0xE000` | `otadata-factory.bin` |
| Application | `0x10000` | `build/relay_client.ino.bin` |

NVS occupies **`0x9000` through `0xDFFF`**. The writes below avoid it.
**Do not flash `WalkieOne06_Application.compact.bin` at zero**: its padding would
erase saved settings. Do not substitute a merged diagnostic image, a different
partition layout, or Arduino's `boot_app0.bin`.

Use a fresh private run directory: never overwrite a previous NVS backup or
retry a failed write blindly. Set `umask 077` before creating logs/backups. Save the following `flash.cfg`
inside the build directory, replacing only the observed USB serial. This is the
reset/halt, uncompressed component-write and hash-verification sequence used in
PCB06 bench sessions. Unlike the failed raw-write attempt on a running CPU,
it explicitly resets into a halted state before touching flash.

```tcl
gdb port disabled
tcl port disabled
telnet port disabled
set ESP_RTOS none
source [find board/esp32s3-builtin.cfg]
adapter serial YOUR_USB_SERIAL
init
targets esp32s3.cpu0
reset init
if {[esp32s3.cpu0 curstate] ne "halted"} {error "CPU0 did not halt"}
foreach tgt [target names] {$tgt configure -work-area-backup 0}
esp compression off
esp encrypt_binary no
flash probe 0
flash read_bank 0 {nvs-before.bin} 0x9000 0x5000
flash write_image erase {build/relay_client.ino.bootloader.bin} 0x0 bin
esp verify_bank_hash 0 {build/relay_client.ino.bootloader.bin} 0x0
flash write_image erase {build/relay_client.ino.partitions.bin} 0x8000 bin
esp verify_bank_hash 0 {build/relay_client.ino.partitions.bin} 0x8000
flash write_image erase {otadata-factory.bin} 0xE000 bin
esp verify_bank_hash 0 {otadata-factory.bin} 0xE000
flash write_image erase {build/relay_client.ino.bin} 0x10000 bin
esp verify_bank_hash 0 {build/relay_client.ino.bin} 0x10000
flash read_bank 0 {nvs-after.bin} 0x9000 0x5000
echo "ALL_COMPONENTS_VERIFIED"
shutdown
```

```sh
umask 077
test ! -e nvs-before.bin && test ! -e nvs-after.bin && \
  "$OPENOCD_ROOT/bin/openocd" -s "$OPENOCD_ROOT/share/openocd/scripts" -f flash.cfg > openocd.log 2>&1
```

Bound this operation to 90 seconds in an automation runner. Require exit 0,
all four successful hash checks and `ALL_COMPONENTS_VERIFIED`. Keep the log
private. Then require `cmp nvs-before.bin nvs-after.bin` to exit 0 (Windows: a
binary comparison such as `fc /b`). A mismatch is a failed preservation check;
stop and retain both private backups. Do not print their contents.

This configuration leaves the CPU halted. **Only after verification and the NVS
comparison**, reconnect OpenOCD with the same disabled ports, board script and
`adapter serial`, then run `init; reset run; shutdown` to boot. Do not use a
reset or unplug to conceal a failed write. If programming fails or times out,
flash can be partial: keep the evidence and diagnose the same board before a
recovery attempt. A printed success line alone is insufficient.

## Verify the running device separately

Check stable USB, expected firmware, saved settings, cellular registration,
PPP, DNS, verified TLS and the authenticated server connection. If an assistant
was available before the write, check it afterward. Test a harmless Talk/Listen
exchange and the changed behavior. Successful flash hashes prove the write;
they do not prove audio, cellular service or battery operation.

A serial monitor can itself reset the device through DTR/RTS. Avoid auto-reset
monitors when investigating disappearing USB. Do not send unknown console
commands or start a hardware test suite by repeatedly pressing Enter.

`restore.py` currently uses **esptool's serial bootloader**, not OpenOCD. It has
no automatic JTAG fallback. If that transport is unavailable, use this guide with
verified components; do not bypass signed-image verification for an official
restore or invent an unavailable recovery download. A fully erased chip needs
its bootloader and partition table too.

## Prompt for an AI assistant

> Read `firmware/README.md` and `firmware/FLASHING.md` first. Confirm PCB06 N16R8
> and the intended operation. Use the observed native USB-JTAG serial with the
> matching Espressif OpenOCD binary/scripts; do not keep retrying serial
> BOOT/RESET. Verify build-manifest hashes and offsets, keep the private NVS
> backup, disable debugger network ports, bound each command, and compare NVS
> after component writes before rebooting. Never erase the whole chip, burn
> eFuses, flash a merged image over NVS, expose credentials, or substitute another
> board. Report flash verification and actual post-boot operation separately.
