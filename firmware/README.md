# Firmware integration

This directory is reserved for the new cellular board's maintained firmware.
There is no validated image, flashing command, release signing key, or recovery
bundle in this repository yet. Do not substitute a build for another board.

The server and Link can be tested without hardware. The next work starts when
the physical device is available:

1. Confirm the exact board, modem, flash layout, wiring, and pinned toolchain.
2. Establish a reproducible clean build and verify USB flashing on that board.
3. Store the owner-selected WSS address, device credential, carrier APN, and TLS
   trust configuration privately. Keep secrets out of the compiled source tree.
4. Connect over cellular with TLS verification enabled. Verify recording, Talk
   and Listen behavior, audio format, reply staging, and exact playback receipts.
5. Repeat after network loss, restart, and interrupted playback; use two devices
   to verify routing and rejection of another device's reply or active turn.
6. Implement and test settings reset, application restore, and full-flash recovery
   separately, including interruption during update. Retain a verified recovery
   path before experimenting with a custom image.
7. Specify signed maintained updates and explicit custom-firmware opt-out. Choosing
   a personal server must not require accepting company cloud service or silently
   replacing a custom build. Company purchase/ownership records remain separate.

Add board source, tools, tests, and these instructions together when each path is
verified. Keep the checked-in source buildable without company keys or download
hosts. Record firmware/toolchain versions and redacted test evidence; distinguish
simulator, provider, public-network, and physical-device results.

The current [device schema](../schema/events.json) and
[personal-server protocol](../personal-server/PROTOCOL.md) are the software
integration targets. Some schema messages describe capabilities the minimal
server does not implement, such as managed firmware delivery.
