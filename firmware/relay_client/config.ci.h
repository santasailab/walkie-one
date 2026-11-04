#pragma once

// Non-secret compile-time values for clean CI builds. The cellular firmware
// does not use Wi-Fi, and every Device credential is provisioned into NVS.
#define WIFI_SSID ""
#define WIFI_PASS ""
#define RELAY_HOST "your-server.example"
#define RELAY_PATH "/device"
