#pragma once

#include <WiFi.h>
#include <Preferences.h>
#include <esp_wifi.h>
#include "wifi_networks.h"

class EspWifiPlatform final : public saved_wifi::Platform {
 public:
  saved_wifi::LoadResult load(saved_wifi::Profiles &profiles) override {
    opened_ = store_.begin("wifi", false);
    if (!opened_) return saved_wifi::LoadResult::Error;
    if (!store_.isKey("profiles")) return saved_wifi::LoadResult::Missing;
    if (store_.getBytesLength("profiles") != sizeof(profiles) ||
        store_.getBytes("profiles", &profiles, sizeof(profiles)) != sizeof(profiles))
      return saved_wifi::LoadResult::Error;
    return saved_wifi::LoadResult::Loaded;
  }
  bool save(const saved_wifi::Profiles &profiles) override {
    return opened_ && store_.putBytes("profiles", &profiles, sizeof(profiles)) == sizeof(profiles);
  }
  bool connected() override { return WiFi.status() == WL_CONNECTED; }
  bool connectedTo(const char *ssid) override { return joining_ && connected() && WiFi.SSID() == ssid; }
  void disconnect() override {
    joining_ = false;
    // Wait for the old connection's event before trying a replacement password
    // for that same SSID. Otherwise stale WL_CONNECTED could save a bad password.
    WiFi.disconnect(false, false, 1000);
  }
  void join(const saved_wifi::Network &network) override {
    // Fail closed if RAM-only radio configuration cannot be selected. Never
    // overwrite the legacy station's NVS while testing a new password.
    joining_ = false;
    if (esp_wifi_set_storage(WIFI_STORAGE_RAM) != ESP_OK) {
      report("ERROR could not select temporary Wi-Fi settings");
      return;
    }
    joining_ = WiFi.begin(network.ssid, network.password[0] ? network.password : nullptr) != WL_CONNECT_FAILED;
  }
  void startScan() override { WiFi.scanNetworks(true, true); }
  bool scanReady() override { return WiFi.scanComplete() != WIFI_SCAN_RUNNING; }
  int16_t strength(const saved_wifi::Network &network) override {
    int16_t best = saved_wifi::kNotSeen;
    for (int i = 0; i < WiFi.scanComplete(); ++i)
      if (WiFi.SSID(i) == network.ssid && WiFi.RSSI(i) > best) best = WiFi.RSSI(i);
    return best;
  }
  void stopScan() override { esp_wifi_scan_stop(); WiFi.scanDelete(); }
  void report(const char *message, const char *ssid = "") override {
    Serial.printf("[wifi] %s%s%s\n", message, *ssid ? " " : "", ssid);
  }

 private:
  Preferences store_;
  bool opened_ = false;
  bool joining_ = false;
};

static EspWifiPlatform wifiPlatform;
static saved_wifi::Manager savedWifi(wifiPlatform);

static void setupSavedWifi() {
  // WiFi.mode() has loaded the old station config from NVS. Read it before
  // selecting RAM storage: neither trying nor forgetting an AP may overwrite
  // that legacy copy. Future boots use our list, including an empty list.
  wifi_config_t legacy = {};
  saved_wifi::Network previous;
  if (esp_wifi_get_config(WIFI_IF_STA, &legacy) == ESP_OK) {
    memcpy(previous.ssid, legacy.sta.ssid, sizeof(legacy.sta.ssid));
    memcpy(previous.password, legacy.sta.password, sizeof(previous.password));
  }
  memset(&legacy, 0, sizeof(legacy));
  WiFi.persistent(false);
  esp_wifi_set_storage(WIFI_STORAGE_RAM);
  WiFi.setAutoReconnect(false); // The manager tries every known AP fairly.
  const bool firstSetup = savedWifi.begin(previous);
  memset(&previous, 0, sizeof(previous));
  // Retain the legacy engineering build option only for an unconfigured unit.
  if (firstSetup && !savedWifi.profiles().count && strlen(WIFI_SSID)) {
    saved_wifi::Network configured;
    if (strlen(WIFI_SSID) < sizeof(configured.ssid) && strlen(WIFI_PASS) < sizeof(configured.password)) {
      strcpy(configured.ssid, WIFI_SSID);
      strcpy(configured.password, WIFI_PASS);
      savedWifi.request(configured, millis());
    }
  }
}
