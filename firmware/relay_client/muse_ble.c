/* Copyright (c) Meta Platforms, Inc. and affiliates.
 * Licensed under the Apache License, Version 2.0.
 * See ../licenses/Apache-2.0.txt.
 * Modified by Santa’s AI Lab for the Walkie One BLE wrapper.
 */

// Adapted from Meta's muse-gadget-sdk esp32/main/ble_server.c (Apache-2.0),
// commit 4268880: the same service, characteristics and advertisement, so
// the Muse app finds this Device as it finds Meta's own ESP32 gadgets.

#include "muse_ble.h"

#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "host/ble_gap.h"
#include "host/ble_gatt.h"
#include "host/ble_hs.h"
#include "host/ble_uuid.h"
#include "host/util/util.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"

static const char *TAG = "muse.ble";

// 7fdd3d1c-38ea-46cf-8b46-314ecf5f240c, little-endian.
static const ble_uuid128_t SERVICE_UUID = BLE_UUID128_INIT(
    0x0c, 0x24, 0x5f, 0xcf, 0x4e, 0x31, 0x46, 0x8b,
    0xcf, 0x46, 0xea, 0x38, 0x1c, 0x3d, 0xdd, 0x7f);
// 4d593029-28a2-4a6e-a1f0-3c2d5e8f9b01: phone writes here.
static const ble_uuid128_t RX_UUID = BLE_UUID128_INIT(
    0x01, 0x9b, 0x8f, 0x5e, 0x2d, 0x3c, 0xf0, 0xa1,
    0x6e, 0x4a, 0xa2, 0x28, 0x29, 0x30, 0x59, 0x4d);
// d75dc4ca-7b2b-4e9c-8f0a-1d2e3f4a5b6c: Device notifies here.
static const ble_uuid128_t TX_UUID = BLE_UUID128_INIT(
    0x6c, 0x5b, 0x4a, 0x3f, 0x2e, 0x1d, 0x0a, 0x8f,
    0x9c, 0x4e, 0x2b, 0x7b, 0xca, 0xc4, 0x5d, 0xd7);

static char s_name[32];
static muse_ble_write_cb s_on_write;
static muse_ble_disconnect_cb s_on_disconnect;
static uint16_t s_rx_handle, s_tx_handle;
static volatile uint16_t s_conn = BLE_HS_CONN_HANDLE_NONE;
static volatile uint16_t s_mtu = 23;
static volatile bool s_subscribed, s_started, s_stopping;
static uint8_t s_last[160];
static size_t s_last_len;

static void advertise(void);

static int rx_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt,
                     void *arg) {
    (void)conn; (void)attr; (void)arg;
    if (s_stopping || ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR) return BLE_ATT_ERR_UNLIKELY;
    uint16_t total = OS_MBUF_PKTLEN(ctxt->om);
    if (total == 0) return 0;
    uint8_t *buf = malloc(total);
    if (!buf) return BLE_ATT_ERR_INSUFFICIENT_RES;
    uint16_t len = 0;
    if (ble_hs_mbuf_to_flat(ctxt->om, buf, total, &len) == 0 && len && s_on_write) {
        s_on_write(buf, len);
    }
    free(buf);
    return 0;
}

static int tx_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt,
                     void *arg) {
    (void)conn; (void)attr; (void)arg;
    if (s_stopping || ctxt->op != BLE_GATT_ACCESS_OP_READ_CHR) return BLE_ATT_ERR_UNLIKELY;
    return os_mbuf_append(ctxt->om, s_last, s_last_len) == 0 ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES;
}

static const struct ble_gatt_chr_def s_chrs[] = {
    {
        .uuid = &RX_UUID.u,
        .access_cb = rx_access,
        .flags = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_NO_RSP,
        .val_handle = &s_rx_handle,
    },
    {
        .uuid = &TX_UUID.u,
        .access_cb = tx_access,
        .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY,
        .val_handle = &s_tx_handle,
    },
    {0},
};

static const struct ble_gatt_svc_def s_svcs[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &SERVICE_UUID.u,
        .characteristics = s_chrs,
    },
    {0},
};

static int gap_event(struct ble_gap_event *event, void *arg) {
    (void)arg;
    switch (event->type) {
        case BLE_GAP_EVENT_CONNECT:
            if (event->connect.status == 0) {
                s_conn = event->connect.conn_handle;
                ESP_LOGI(TAG, "phone connected");
            } else if (!s_stopping) {
                advertise();
            }
            break;
        case BLE_GAP_EVENT_DISCONNECT:
            ESP_LOGI(TAG, "phone disconnected reason=%d", event->disconnect.reason);
            s_conn = BLE_HS_CONN_HANDLE_NONE;
            s_subscribed = false;
            s_mtu = 23;
            if (s_on_disconnect) s_on_disconnect();
            if (!s_stopping) advertise();
            break;
        case BLE_GAP_EVENT_SUBSCRIBE:
            if (event->subscribe.attr_handle == s_tx_handle) {
                s_subscribed = event->subscribe.cur_notify;
            }
            break;
        case BLE_GAP_EVENT_MTU:
            s_mtu = event->mtu.value;
            break;
        default:
            break;
    }
    return 0;
}

static void advertise(void) {
    if (s_stopping || s_conn != BLE_HS_CONN_HANDLE_NONE) return;
    // Flags, the 128-bit service UUID and manufacturer data 0xFFFF plus a
    // not-yet-paired byte: what the Muse app scans for. The name goes in the
    // scan response so everything fits the 31-byte legacy advertisement.
    static uint8_t mfg[] = {0xFF, 0xFF, 0x00};
    struct ble_hs_adv_fields adv;
    memset(&adv, 0, sizeof(adv));
    adv.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
    adv.uuids128 = (ble_uuid128_t *)&SERVICE_UUID;
    adv.num_uuids128 = 1;
    adv.uuids128_is_complete = 1;
    adv.mfg_data = mfg;
    adv.mfg_data_len = sizeof(mfg);
    int rc = ble_gap_adv_set_fields(&adv);
    if (rc != 0) {
        ESP_LOGE(TAG, "adv_set_fields rc=%d", rc);
        return;
    }
    struct ble_hs_adv_fields rsp;
    memset(&rsp, 0, sizeof(rsp));
    rsp.name = (uint8_t *)s_name;
    rsp.name_len = strlen(s_name);
    rsp.name_is_complete = 1;
    ble_gap_adv_rsp_set_fields(&rsp);
    struct ble_gap_adv_params params;
    memset(&params, 0, sizeof(params));
    params.conn_mode = BLE_GAP_CONN_MODE_UND;
    params.disc_mode = BLE_GAP_DISC_MODE_GEN;
    rc = ble_gap_adv_start(BLE_OWN_ADDR_PUBLIC, NULL, BLE_HS_FOREVER, &params,
                           gap_event, NULL);
    if (rc != 0 && rc != BLE_HS_EALREADY) ESP_LOGE(TAG, "adv_start rc=%d", rc);
}

static void on_sync(void) {
    if (ble_hs_util_ensure_addr(0) == 0) advertise();
}

static void on_reset(int reason) {
    ESP_LOGW(TAG, "host reset reason=%d", reason);
}

static void host_task(void *arg) {
    (void)arg;
    nimble_port_run();
    nimble_port_freertos_deinit();
}

bool muse_ble_begin(const char *name, muse_ble_write_cb on_write,
                    muse_ble_disconnect_cb on_disconnect) {
    if (s_started) return true;
    strncpy(s_name, name, sizeof(s_name) - 1);
    s_on_write = on_write;
    s_on_disconnect = on_disconnect;
    s_stopping = false;
    s_conn = BLE_HS_CONN_HANDLE_NONE;
    s_subscribed = false;
    s_mtu = 23;
    s_last_len = 0;
    if (nimble_port_init() != ESP_OK) {
        ESP_LOGE(TAG, "nimble_port_init failed");
        return false;
    }
    ble_svc_gap_init();
    ble_svc_gatt_init();
    if (ble_gatts_count_cfg(s_svcs) != 0 || ble_gatts_add_svcs(s_svcs) != 0) {
        ESP_LOGE(TAG, "GATT registration failed");
        nimble_port_deinit();
        return false;
    }
    ble_svc_gap_device_name_set(s_name);
    ble_hs_cfg.sync_cb = on_sync;
    ble_hs_cfg.reset_cb = on_reset;
    nimble_port_freertos_init(host_task);
    s_started = true;
    return true;
}

void muse_ble_end(void) {
    if (!s_started) return;
    s_stopping = true;
    ble_gap_adv_stop();
    if (s_conn != BLE_HS_CONN_HANDLE_NONE) {
        ble_gap_terminate(s_conn, BLE_ERR_REM_USER_CONN_TERM);
        vTaskDelay(pdMS_TO_TICKS(200));
    }
    int rc = -1;
    for (int attempt = 0; attempt < 3 && (rc = nimble_port_stop()) != 0; attempt++) {
        vTaskDelay(pdMS_TO_TICKS(200));
    }
    if (rc != 0) {
        // Deinit under a live host task corrupts it; Meta reboots here too.
        ESP_LOGE(TAG, "nimble_port_stop failed; restarting");
        esp_restart();
    }
    nimble_port_deinit();
    s_conn = BLE_HS_CONN_HANDLE_NONE;
    s_subscribed = false;
    s_started = false;
}

bool muse_ble_notify(const uint8_t *data, size_t len) {
    if (!s_started || s_stopping || !s_subscribed || s_conn == BLE_HS_CONN_HANDLE_NONE) {
        return false;
    }
    s_last_len = len < sizeof(s_last) ? len : sizeof(s_last);
    memcpy(s_last, data, s_last_len);
    struct os_mbuf *om = ble_hs_mbuf_from_flat(data, len);
    return om && ble_gatts_notify_custom(s_conn, s_tx_handle, om) == 0;
}

uint16_t muse_ble_mtu(void) { return s_mtu; }

bool muse_ble_connected(void) { return s_conn != BLE_HS_CONN_HANDLE_NONE; }

static void disconnect_task(void *arg) {
    vTaskDelay(pdMS_TO_TICKS((uint32_t)(uintptr_t)arg));
    if (s_started && !s_stopping && s_conn != BLE_HS_CONN_HANDLE_NONE) {
        ble_gap_terminate(s_conn, BLE_ERR_REM_USER_CONN_TERM);
    }
    vTaskDelete(NULL);
}

void muse_ble_disconnect_after(uint32_t ms) {
    xTaskCreate(disconnect_task, "muse_dc", 2048, (void *)(uintptr_t)ms, 4, NULL);
}
