#pragma once
// The 06 board's over-the-air installer: the NVS record, an HTTPS download
// into the spare app slot with SHA-256 and Ed25519 checks, and the boot switch.
// ota_policy.h decides when; relay_client.ino wires the two together.
#include <Preferences.h>
#include <esp_crt_bundle.h>
#include <esp_http_client.h>
#include <esp_ota_ops.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <mbedtls/base64.h>
#include <mbedtls/sha256.h>
#include <sodium.h>

#include "ota_policy.h"
#include "release_key.h"  // written by tools/build_pcb06.py from release-key.pub

namespace pcb06_ota {

struct Image {
  char version[16];
  char url[513];
  char sha256[65];
  char signature[96];
  int size;
};
enum class Download : uint8_t { Idle, Running, Staged, Failed, Stopped };

inline volatile Download state = Download::Idle;
inline volatile bool stopRequested = false;
inline Image job;

// One NVS blob, so an interrupted write leaves the old record or the new one.
inline OtaRecord load() {
  OtaRecord record;
  Preferences nvs;
  if (nvs.begin("ota", true)) {
    if (nvs.getBytesLength("record") != sizeof record ||
        nvs.getBytes("record", &record, sizeof record) != sizeof record ||
        record.layout != OtaRecord{}.layout)
      record = OtaRecord{};
    nvs.end();
  }
  return record;
}

inline bool save(const OtaRecord &record) {
  Preferences nvs;
  if (!nvs.begin("ota", false)) return false;
  const bool ok = nvs.putBytes("record", &record, sizeof record) == sizeof record;
  nvs.end();
  return ok;
}

// The release key signed this board, version and SHA-256. A build without a
// published key accepts nothing.
inline bool signedByRelease(const Image &image) {
  uint8_t key[32], signature[64];
  size_t length = 0;
  const char *keyText = RELAY_RELEASE_KEY;
  if (mbedtls_base64_decode(key, sizeof key, &length, (const uint8_t *)keyText,
                            strlen(keyText)) || length != sizeof key)
    return false;
  if (mbedtls_base64_decode(signature, sizeof signature, &length,
                            (const uint8_t *)image.signature, strlen(image.signature)) ||
      length != sizeof signature)
    return false;
  char message[128];
  const int size = otaSignedMessage(message, sizeof message, "06", image.version, image.sha256);
  return sodium_init() >= 0 && size > 0 && size < int(sizeof message) &&
         crypto_sign_verify_detached(signature, (const uint8_t *)message, size, key) == 0;
}

// Streams the image into the spare slot, hashing what it writes. The boot
// slot is untouched: a staged image only runs after install() and a restart.
inline void downloadTask(void *) {
  Download outcome = Download::Failed;
  const esp_partition_t *slot = esp_ota_get_next_update_partition(nullptr);
  esp_http_client_config_t config = {};
  config.url = job.url;
  config.crt_bundle_attach = esp_crt_bundle_attach;
  config.timeout_ms = 20000;
  config.buffer_size = 4096;
  esp_http_client_handle_t http = esp_http_client_init(&config);
  uint8_t *buffer = (uint8_t *)malloc(4096);
  mbedtls_sha256_context sha;
  mbedtls_sha256_init(&sha);
  mbedtls_sha256_starts(&sha, 0);
  esp_ota_handle_t ota = 0;
  bool open = false;
  int written = 0;
  if (slot && http && buffer && job.size > 0 && uint32_t(job.size) <= slot->size &&
      esp_http_client_open(http, 0) == ESP_OK &&
      esp_http_client_fetch_headers(http) == job.size &&
      esp_http_client_get_status_code(http) == 200 &&
      esp_ota_begin(slot, OTA_WITH_SEQUENTIAL_WRITES, &ota) == ESP_OK) {
    open = true;
    while (written < job.size && !stopRequested) {
      const int want = job.size - written < 4096 ? job.size - written : 4096;
      const int got = esp_http_client_read(http, (char *)buffer, want);
      if (got <= 0) break;
      mbedtls_sha256_update(&sha, buffer, got);
      if (esp_ota_write(ota, buffer, got) != ESP_OK) break;
      written += got;
    }
  }
  uint8_t digest[32];
  char hex[65];
  mbedtls_sha256_finish(&sha, digest);
  mbedtls_sha256_free(&sha);
  for (int i = 0; i < 32; ++i) snprintf(hex + i * 2, 3, "%02x", digest[i]);
  if (stopRequested) {
    outcome = Download::Stopped;
  } else if (open && written == job.size && !strcmp(hex, job.sha256)) {
    open = false;
    if (esp_ota_end(ota) == ESP_OK) outcome = Download::Staged;  // also checks the image format
  }
  if (open) esp_ota_abort(ota);
  if (http) {
    esp_http_client_close(http);
    esp_http_client_cleanup(http);
  }
  free(buffer);
  Serial.printf("[ota] download %s: %d of %d bytes\n",
                outcome == Download::Staged ? "staged" : outcome == Download::Stopped ? "stopped" : "failed",
                written, job.size);
  state = outcome;
  vTaskDelete(nullptr);
}

inline bool start(const Image &image) {
  if (state == Download::Running) return false;
  job = image;
  stopRequested = false;
  state = Download::Running;
  // 8 KiB: TLS handshakes run on this stack. Core 1 at low priority keeps it
  // off the audio task's core and behind the loop.
  if (xTaskCreatePinnedToCore(downloadTask, "ota", 8192, nullptr, 1, nullptr, 1) != pdPASS) {
    state = Download::Failed;
    return false;
  }
  return true;
}

// Boots the staged image next restart. esp_ota_set_boot_partition verifies it.
inline bool install() {
  const esp_partition_t *slot = esp_ota_get_next_update_partition(nullptr);
  return slot && esp_ota_set_boot_partition(slot) == ESP_OK;
}

// A newly installed image runs "pending verify" until it marks itself valid;
// any restart before that boots the previous image.
inline bool unproven() {
  esp_ota_img_states_t image;
  return esp_ota_get_state_partition(esp_ota_get_running_partition(), &image) == ESP_OK &&
         image == ESP_OTA_IMG_PENDING_VERIFY;
}
inline void keep() { esp_ota_mark_app_valid_cancel_rollback(); }
inline void rollBack() { esp_ota_mark_app_invalid_rollback_and_reboot(); }

}  // namespace pcb06_ota
