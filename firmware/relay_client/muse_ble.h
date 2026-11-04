#pragma once

// Bluetooth LE peripheral for Muse gadget pairing, adapted from Meta's
// muse-gadget-sdk esp32/main/ble_server.c (Apache-2.0). It only moves bytes;
// muse_pairing.h owns the protocol. Device only.

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef void (*muse_ble_write_cb)(const uint8_t *data, size_t len);
typedef void (*muse_ble_disconnect_cb)(void);

// Starts NimBLE and advertises `name` until muse_ble_end().
bool muse_ble_begin(const char *name, muse_ble_write_cb on_write,
                    muse_ble_disconnect_cb on_disconnect);
// Stops advertising, drops the phone and releases the host.
void muse_ble_end(void);
bool muse_ble_notify(const uint8_t *data, size_t len);
uint16_t muse_ble_mtu(void);
bool muse_ble_connected(void);
void muse_ble_disconnect_after(uint32_t ms);

#ifdef __cplusplus
}
#endif
