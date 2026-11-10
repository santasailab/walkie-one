#include "cellular.h"

#include <Arduino.h>
#ifdef RELAY_BOARD_06

#include "cellular_policy.h"
#include <HardwareSerial.h>
#include <Network.h>
#include <PPP.h>
#include <Preferences.h>
#include <atomic>
#include <climits>

namespace {

using cellular_policy::Config;
constexpr int kSupply = 21, kKey = 14, kReset = 15, kUsbBoot = 42;
constexpr int kDtr = 16, kMux = 39, kTx = 17, kRx = 18;
constexpr uint32_t kReadyMs = 20000, kRegisterMs = 90000, kIpMs = 30000;
constexpr uint32_t kOffMs = 3000;
enum class State {
  Off, Starting, Sim, Baud, Registering, Dialing, Online, Backoff,
  StorageError, NoAt, SimLocked, NetworkTimeError, ApnError, BaudError, AttachError,
  PppError, Restarting, ShutDown
};

portMUX_TYPE configLock = portMUX_INITIALIZER_UNLOCKED;
Config desired = cellular_policy::emptyConfig();
uint32_t desiredGeneration = 1;
uint32_t storedGeneration = 0;  // the generation setup read from storage
TaskHandle_t workerTask = nullptr;
std::atomic<State> state{State::Off};
std::atomic<bool> dataUp{false}, restartRequested{false}, shutdownRequested{false};
std::atomic<bool> supplyLive{false};
std::atomic<int> cachedRssi{-1}, negotiatedBaud{0};
std::atomic<int> advertisedBaud{0};
std::atomic<int> probeBaud{0};
std::atomic<uint32_t> receivedBytes{0};
std::atomic<unsigned> attemptCount{0};
HardwareSerial modemUart(1);
bool rawOpen = false, pppOpen = false, initialized = false;
uint32_t lastSupplyOff = 0;
int64_t networkUtc = 0;      // protected by configLock, with networkUtcAt
uint32_t networkUtcAt = 0;

const char *stateName(State value) {
  switch (value) {
    case State::Off: return "off";
    case State::Starting: return "waiting for modem AT";
    case State::Sim: return "waiting for SIM";
    case State::Baud: return "verifying UART speed";
    case State::Registering: return "waiting for network attachment";
    case State::Dialing: return "waiting for PPP address";
    case State::Online: return "PPP data up";
    case State::Backoff: return "retry wait";
    case State::StorageError: return "configuration could not be saved; radio off";
    case State::NoAt: return "no AT response";
    case State::SimLocked: return "SIM missing, locked or not ready";
    case State::NetworkTimeError: return "modem network-time update could not be disabled";
    case State::ApnError: return "modem rejected mobile data profile";
    case State::BaudError: return "UART capability or speed verification failed";
    case State::AttachError: return "network attachment timeout";
    case State::PppError: return "PPP failed or disconnected";
    case State::Restarting: return "restart requested; data session ended";
    case State::ShutDown: return "powered down for switch-off";
  }
  return "unknown";
}

Config snapshot(uint32_t &generation) {
  portENTER_CRITICAL(&configLock);
  Config copy = desired;
  generation = desiredGeneration;
  portEXIT_CRITICAL(&configLock);
  return copy;
}

bool wanted(uint32_t generation) {
  portENTER_CRITICAL(&configLock);
  const bool value = desired.enabled && generation == desiredGeneration;
  portEXIT_CRITICAL(&configLock);
  return value && !shutdownRequested.load();
}

// Only the worker touches UART, PPP or the modem's pins. A STOP/configuration
// change is noticed by the worker, which then shuts the modem down in order.
void pinsOff() {
  digitalWrite(kKey, LOW);
  digitalWrite(kSupply, LOW);
  digitalWrite(kReset, LOW);
  digitalWrite(kUsbBoot, LOW);
  digitalWrite(kDtr, LOW);
  digitalWrite(kMux, LOW);
  supplyLive.store(false);
}

bool powerPin(int pin, int level, uint32_t generation) {
  portENTER_CRITICAL(&configLock);
  // Serialize supply-on with shutdown: once the caller observes powered-down,
  // this worker must not energize the modem after shutdown reports completion.
  const bool allow = desired.enabled && generation == desiredGeneration &&
                     !shutdownRequested.load();
  if (allow) {
    digitalWrite(pin, level);
    if (pin == kSupply) supplyLive.store(level == HIGH);
  }
  portEXIT_CRITICAL(&configLock);
  return allow;
}

// Removing the supply from a running module risks its flash and the SIM.
// The PWRKEY gesture works in AT and data mode alike and needs no reply, so
// it is used whatever state the UART is in. This wait is unconditional: a
// STOP must not cut it short.
void powerOffModem() {
  digitalWrite(kKey, HIGH);
  vTaskDelay(pdMS_TO_TICKS(cellular_policy::kPowerOffHoldMs));
  digitalWrite(kKey, LOW);
  vTaskDelay(pdMS_TO_TICKS(cellular_policy::kPowerOffSettleMs));
}

bool waitFor(uint32_t duration, uint32_t generation) {
  const uint32_t start = millis();
  while (!cellular_policy::expired(millis(), start, duration)) {
    if (!wanted(generation)) return false;
    vTaskDelay(pdMS_TO_TICKS(10));
  }
  return wanted(generation);
}

void cleanup() {
  dataUp.store(false);
  if (pppOpen) { PPP.end(); pppOpen = false; }
  if (supplyLive.load()) powerOffModem();
  pinsOff();
  lastSupplyOff = millis();
  if (rawOpen) { modemUart.end(); rawOpen = false; }
  pinMode(kTx, INPUT);
  pinMode(kRx, INPUT);
  cachedRssi.store(-1);
}

// Used only before PPP owns the UART. Responses are bounded and never logged:
// SIM identities and network credentials must not become serial diagnostics.
bool rawAt(const char *command, String &response, uint32_t timeout,
           uint32_t generation) {
  response = "";
  unsigned discarded = 0;
  while (modemUart.available() && discarded++ < 4096) {
    modemUart.read();
    receivedBytes.fetch_add(1);
  }
  if (modemUart.available()) return false;
  if (!wanted(generation)) return false;
  modemUart.print(command);
  modemUart.print("\r");
  const uint32_t start = millis();
  while (!cellular_policy::expired(millis(), start, timeout)) {
    if (!wanted(generation)) return false;
    while (modemUart.available()) {
      const char c = static_cast<char>(modemUart.read());
      receivedBytes.fetch_add(1);
      if (response.length() >= 2048) return false;
      response += c;
    }
    const auto result = cellular_policy::terminalResult(response.c_str(), response.length());
    if (result == cellular_policy::AtResult::Ok) return true;
    if (result == cellular_policy::AtResult::Error) return false;
    vTaskDelay(pdMS_TO_TICKS(10));
  }
  return false;
}

int discoverBaud(uint32_t generation) {
  modemUart.setRxBufferSize(4096);
  modemUart.begin(115200, SERIAL_8N1, kRx, kTx);
  probeBaud.store(115200);
  rawOpen = true;
  const uint32_t start = millis();
  String response;
  // Match the successful diagnostic first: hold 115200 for the entire startup
  // window, allow one second per AT reply, then leave 500 ms between attempts.
  while (!cellular_policy::expired(millis(), start, kReadyMs) && wanted(generation)) {
    if (rawAt("AT", response, 1000, generation)) return 115200;
    if (cellular_policy::expired(millis(), start, kReadyMs) ||
        !waitFor(500, generation)) break;
  }
  // A previously configured modem can boot at another fixed rate. Probe each
  // alternative only after the stable default-rate window; total wait is bounded.
  for (const int baud : {921600, 460800, 230400}) {
    if (!wanted(generation)) return 0;
    modemUart.updateBaudRate(baud);
    probeBaud.store(baud);
    if (!waitFor(20, generation)) return 0;
    if (rawAt("AT", response, 1000, generation)) return baud;
  }
  return 0;
}

bool disableNetworkTimeUpdates(uint32_t generation) {
  String response;
  if (!rawAt("AT+CTZU?", response, 5000, generation)) return false;
  const int mode = cellular_policy::taggedNumber(response.c_str(), "+CTZU:");
  if (mode == 0) return true;
  // CTZU is persistent. Read first to avoid rewriting a repaired modem's flash.
  if (mode < 0 || !rawAt("AT+CTZU=0", response, 5000, generation)) return false;
  return rawAt("AT+CTZU?", response, 5000, generation) &&
         cellular_policy::taggedNumber(response.c_str(), "+CTZU:") == 0;
}

bool commandAt(const char *command, String &response, uint32_t generation,
               int timeout = 1000) {
  // Never query signal, attachment, identities or any AT command in DATA mode.
  return wanted(generation) && PPP.mode() == ESP_MODEM_MODE_COMMAND &&
         PPP.cmd(command, response, timeout) && wanted(generation);
}

bool connectAttempt(const Config &config, uint32_t generation, int &baudCeiling) {
  const uint32_t offFor = millis() - lastSupplyOff;
  if (offFor < kOffMs && !waitFor(kOffMs - offFor, generation)) return false;
  state.store(State::Starting);
  if (!powerPin(kSupply, HIGH, generation) || !waitFor(200, generation) ||
      !powerPin(kKey, HIGH, generation) || !waitFor(100, generation)) return false;
  powerPin(kKey, LOW, generation);
  const int initialBaud = discoverBaud(generation);
  if (!initialBaud) { state.store(State::NoAt); return false; }
  String response;
  if (!rawAt("ATE0", response, 1000, generation) ||
      !rawAt("AT+IFC=0,0", response, 1000, generation)) {
    state.store(State::NoAt); return false;
  }
  state.store(State::Sim);
  const uint32_t simStart = millis();
  bool simReady = false;
  while (!cellular_policy::expired(millis(), simStart, kReadyMs) && wanted(generation)) {
    if (rawAt("AT+CPIN?", response, 1000, generation)) {
      if (response.indexOf("+CPIN: READY") >= 0) { simReady = true; break; }
      if (response.indexOf("SIM PIN") >= 0 || response.indexOf("SIM PUK") >= 0) break;
    }
    if (!waitFor(500, generation)) return false;
  }
  if (!simReady) { state.store(State::SimLocked); return false; }
  // LTE attachment uses the modem's stored PDP context. PPP.setApn() only
  // prepares a later dial, so apply the selected APN before enabling the radio.
  char profileCommand[cellular_policy::kMaxApn + 40];
  snprintf(profileCommand, sizeof(profileCommand), "AT+CGDCONT=1,\"IP\",\"%s\"", config.apn);
  if (!rawAt("AT+CFUN=4", response, 25000, generation)) {
    state.store(State::ApnError); return false;
  }
  // Network time updates can trigger a SIM7672 flash-write assertion on attach.
  // Keep automatic updates disabled; a readable modem clock is only a best-effort
  // seed for the ESP32, which synchronizes independently through SNTP.
  if (!disableNetworkTimeUpdates(generation)) {
    state.store(State::NetworkTimeError); return false;
  }
  if (!rawAt(profileCommand, response, 5000, generation) ||
      !rawAt("AT+CFUN=1", response, 25000, generation)) {
    state.store(State::ApnError); return false;
  }
  state.store(State::Baud);
  if (!rawAt("AT+IPR=?", response, 1000, generation)) {
    state.store(State::BaudError); return false;
  }
  advertisedBaud.store(cellular_policy::preferredBaud(response.c_str()));
  const int targetBaud = cellular_policy::preferredBaud(response.c_str(), baudCeiling);
  if (!targetBaud) { state.store(State::BaudError); return false; }
  // Verify the new rate while the raw UART still owns the port, before
  // handing it to PPP. Do not rely on an acknowledged rate change alone.
  if (targetBaud != initialBaud) {
    char baudCommand[32];
    snprintf(baudCommand, sizeof(baudCommand), "AT+IPR=%d\r", targetBaud);
    modemUart.print(baudCommand);
    modemUart.flush();
    if (!waitFor(100, generation)) return false;
    modemUart.updateBaudRate(targetBaud);
    if (!waitFor(20, generation)) return false;
    if (!rawAt("AT", response, 1000, generation)) {
      baudCeiling = cellular_policy::slowerBaud(targetBaud);
      state.store(State::BaudError); return false;
    }
  }
  modemUart.end();
  rawOpen = false;
  if (!wanted(generation)) return false;
  // Start PPP at the rate already verified with the modem.
  PPP.setResetPin(-1);
  PPP.sendAtBurst(false);
  pppOpen = true;  // Also clean up a partially initialized begin().
  if (!PPP.setApn(config.apn) ||
      !PPP.setPins(kTx, kRx, -1, -1, ESP_MODEM_FLOW_CONTROL_NONE) ||
      !PPP.begin(PPP_MODEM_GENERIC, 1, targetBaud) || !wanted(generation)) {
    state.store(State::PppError); return false;
  }
  if (!commandAt("AT", response, generation)) {
    baudCeiling = cellular_policy::slowerBaud(targetBaud);
    state.store(State::BaudError); return false;
  }
  negotiatedBaud.store(targetBaud);
  state.store(State::Registering);
  const uint32_t attachStart = millis();
  bool attached = false;
  while (!cellular_policy::expired(millis(), attachStart, kRegisterMs) && wanted(generation)) {
    if (commandAt("AT+CGATT?", response, generation) &&
        cellular_policy::taggedNumber(response.c_str(), "+CGATT:") == 1) {
      attached = true; break;
    }
    if (!waitFor(1000, generation)) return false;
  }
  if (!attached) { state.store(State::AttachError); return false; }
  if (commandAt("AT+CSQ", response, generation))
    cachedRssi.store(cellular_policy::signalDbm(response.c_str()));
  // Read the modem clock when available without enabling automatic network
  // updates. Its default/unset value leaves TLS to SNTP and the build stamp.
  for (int look = 0; look < 3; ++look) {
    if (look && !waitFor(500, generation)) return false;
    if (!commandAt("AT+CCLK?", response, generation)) break;
    const int64_t utc = cellular_policy::networkTimeUtc(response.c_str());
    if (!utc) continue;
    portENTER_CRITICAL(&configLock);
    networkUtc = utc;
    networkUtcAt = millis();
    portEXIT_CRITICAL(&configLock);
    break;
  }
  if (!wanted(generation)) return false;
  state.store(State::Dialing);
  if (!PPP.mode(ESP_MODEM_MODE_DATA) || !wanted(generation)) {
    state.store(State::PppError); return false;
  }
  const uint32_t ipStart = millis();
  while (!PPP.hasIP() && !cellular_policy::expired(millis(), ipStart, kIpMs)) {
    if (!waitFor(100, generation)) return false;
  }
  if (!wanted(generation) || !PPP.hasIP() || !PPP.setDefault()) {
    state.store(State::PppError); return false;
  }
  portENTER_CRITICAL(&configLock);
  const bool stillEnabled = desired.enabled && desiredGeneration == generation &&
                            !shutdownRequested.load();
  if (stillEnabled) dataUp.store(true);
  portEXIT_CRITICAL(&configLock);
  if (!stillEnabled) return false;
  state.store(State::Online);
  return true;
}

bool persist(const Config &config) {
  Preferences preferences;
  if (!preferences.begin("cellular", false)) return false;
  const bool saved = preferences.putBytes("config", &config, sizeof(config)) == sizeof(config);
  preferences.end();
  return saved;
}

void worker(void *) {
  uint32_t activeGeneration = 0;
  Config config = cellular_policy::emptyConfig();
  unsigned attempts = 0;
  int baudCeiling = 921600;
  for (;;) {
    uint32_t generation;
    Config next = snapshot(generation);
    if (generation != activeGeneration) {
      cleanup();
      activeGeneration = generation;
      config = next;
      attempts = 0;
      baudCeiling = 921600;
      attemptCount.store(0);
      negotiatedBaud.store(0);
      advertisedBaud.store(0);
      if (generation == storedGeneration) {
        state.store(State::Off);  // what setup read from storage is stored already
      } else if (!persist(config)) {
        config.enabled = false;
        state.store(State::StorageError);
        portENTER_CRITICAL(&configLock);
        if (desiredGeneration == generation) desired.enabled = false;
        portEXIT_CRITICAL(&configLock);
      } else {
        Serial.printf("[cell] settings saved; auto-start=%s\n", config.enabled ? "on" : "off");
        state.store(State::Off);
      }
    }
    if (!config.enabled || !config.apn[0]) {
      vTaskDelay(pdMS_TO_TICKS(50));
      continue;
    }
    if (shutdownRequested.load()) {  // switching off: down until the restart
      state.store(State::ShutDown);
      vTaskDelay(pdMS_TO_TICKS(50));
      continue;
    }
    if (attempts < UINT_MAX) ++attempts;
    attemptCount.store(attempts);
    // A request left over from an earlier session is forgotten here, before
    // this one reports data: one made after that must not be lost.
    restartRequested.store(false);
    if (connectAttempt(config, activeGeneration, baudCeiling)) {
      const uint32_t connectedAt = millis();
      while (wanted(activeGeneration) && PPP.hasIP() && !restartRequested.load())
        vTaskDelay(pdMS_TO_TICKS(50));
      if (cellular_policy::expired(millis(), connectedAt, 30000)) attempts = 0;
      if (wanted(activeGeneration))
        state.store(restartRequested.exchange(false) ? State::Restarting : State::PppError);
    }
    const State failure = state.load();
    cleanup();
    if (!wanted(activeGeneration)) continue;
    const uint32_t wait = cellular_policy::retryDelayMs(attempts);
    Serial.printf("[cell] attempt %u: %s; retry in %lu s\n", attempts, stateName(failure),
                  static_cast<unsigned long>(wait / 1000));
    state.store(State::Backoff);
    waitFor(wait, activeGeneration);
  }
}

// The session is over for callers at once; the worker notices the new
// generation and powers the module down in order before touching its pins.
void queueConfig(const Config &config) {
  portENTER_CRITICAL(&configLock);
  desired = config;
  ++desiredGeneration;
  dataUp.store(false);
  portEXIT_CRITICAL(&configLock);
}

}  // namespace

bool cellularSetup() {
  if (initialized) return true;
  for (const int pin : {kSupply, kKey, kReset, kUsbBoot, kDtr, kMux}) {
    digitalWrite(pin, LOW);
    pinMode(pin, OUTPUT);
  }
  pinsOff();
  lastSupplyOff = millis();
  Preferences preferences;
  if (preferences.begin("cellular", true)) {
    Config saved{};
    if (preferences.getBytesLength("config") == sizeof(saved) &&
        preferences.getBytes("config", &saved, sizeof(saved)) == sizeof(saved) &&
        cellular_policy::validConfig(saved)) desired = saved;
    preferences.end();
  }
  storedGeneration = desiredGeneration;
  if (!Network.begin()) return false;
  if (xTaskCreate(worker, "cellular", 6144, nullptr, 2, &workerTask) != pdPASS) return false;
  initialized = true;
  Serial.println("[cell] PCB06 UART worker ready; CELL APN <value>, CELL START/STOP/STATUS");
  return true;
}

void cellularService() {
  static int announced = -1;
  const int current = static_cast<int>(state.load());
  if (current != announced) {
    Serial.printf("[cell] %s\n", stateName(static_cast<State>(current)));
    announced = current;
  }
}

bool cellularConnected() { return dataUp.load(); }
void cellularRestart() { restartRequested.store(true); }
void cellularShutdown() {
  portENTER_CRITICAL(&configLock);
  shutdownRequested.store(true);
  dataUp.store(false);
  portEXIT_CRITICAL(&configLock);
}
bool cellularPoweredDown() { return !supplyLive.load(); }

// Runs on from the last reading. After 49 days without a new session millis()
// wraps and this falls behind; SNTP remains authoritative once synchronized.
bool cellularNetworkTime(int64_t &utcSeconds) {
  portENTER_CRITICAL(&configLock);
  const int64_t utc = networkUtc;
  const uint32_t at = networkUtcAt;
  portEXIT_CRITICAL(&configLock);
  if (!utc) return false;
  utcSeconds = utc + static_cast<uint32_t>(millis() - at) / 1000;
  return true;
}
int cellularRssiDbm() { return dataUp.load() ? cachedRssi.load() : -1; }

void cellularPrintStatus() {
  uint32_t generation;
  const Config config = snapshot(generation);
  Serial.printf("[cell] %s; enabled=%s APN=%s UART=%d advertised-max=%d probe-baud=%d RXbytes=%lu data=%s attempt=%u RSSI=%d (cached before PPP)\n",
      stateName(state.load()), config.enabled ? "yes" : "no",
      config.apn[0] ? "configured" : "missing", negotiatedBaud.load(), advertisedBaud.load(),
      probeBaud.load(), static_cast<unsigned long>(receivedBytes.load()),
      dataUp.load() ? "up" : "down", attemptCount.load(), cachedRssi.load());
  if (negotiatedBaud.load() > 0 && negotiatedBaud.load() < 921600)
    Serial.println("[cell] UART below live 24kHz PCM rate; buffered audio required");
}

bool cellularCommand(const String &command) {
  if (command != "CELL" && !command.startsWith("CELL ")) return false;
  if (!initialized) { Serial.println("[cell] cellular worker not initialized"); return true; }
  uint32_t generation;
  Config config = snapshot(generation);
  if (command.startsWith("CELL APN ")) {
    const String apn = command.substring(9);
    if (apn.length() > cellular_policy::kMaxApn ||
        std::strlen(apn.c_str()) != apn.length() ||
        !cellular_policy::validApn(apn.c_str())) {
      Serial.println("[cell] invalid APN; use 1-100 letters, digits, dots or hyphens");
      return true;
    }
    std::memset(config.apn, 0, sizeof(config.apn));
    std::memcpy(config.apn, apn.c_str(), apn.length());
    config.enabled = false;
    queueConfig(config);
    Serial.println("[cell] APN save queued; radio off. CELL START enables and saves");
  } else if (command == "CELL START") {
    if (!config.apn[0]) Serial.println("[cell] set CELL APN <value> first");
    else {
      config.enabled = true;
      queueConfig(config);
      Serial.println("[cell] start/save queued; enabled setting survives restart");
    }
  } else if (command == "CELL STOP") {
    config.enabled = false;
    queueConfig(config);
    Serial.println("[cell] radio supply off; stop/save queued");
  } else if (command == "CELL STATUS") cellularPrintStatus();
  else Serial.println("[cell] CELL APN <value> | CELL START | CELL STOP | CELL STATUS");
  return true;
}

#else
#include <Network.h>
#include <esp_event.h>
#include <esp_netif.h>
#include <esp_netif_ppp.h>
#include <iot_usbh_cdc.h>
#include <iot_usbh_modem.h>

namespace {

volatile bool gotIp = false;
bool installed = false;
bool announced = false;

// The SIM7670G enumerated on the production board as 05c6:9330. Its USB
// descriptor labels interface 6 "ppp" and interface 2 "at". Keeping both
// open means PPP data and modem diagnostics do not fight for one serial port.
const usb_modem_id_t modemIds[] = {
  {{USB_DEVICE_ID_MATCH_VID_PID, 0x05c6, 0x9330}, 6, 2,
   "SIMCom SIM7670G"},
  {},
};

void onIpEvent(void *, esp_event_base_t base, int32_t id, void *) {
  if (base != IP_EVENT) return;
  if (id == IP_EVENT_PPP_GOT_IP) {
    gotIp = true;
    esp_netif_t *netif = usbh_modem_get_netif();
    if (netif) esp_netif_set_default_netif(netif);
  } else if (id == IP_EVENT_PPP_LOST_IP) {
    gotIp = false;
    announced = false;
  }
}

}  // namespace

bool cellularSetup() {
  if (installed) return true;
  if (!Network.begin()) return false;

  esp_err_t err = esp_event_handler_register(
      IP_EVENT, IP_EVENT_PPP_GOT_IP, onIpEvent, nullptr);
  if (err != ESP_OK) {
    Serial.printf("[cell] IP event registration failed: %s\n",
                  esp_err_to_name(err));
    return false;
  }
  err = esp_event_handler_register(
      IP_EVENT, IP_EVENT_PPP_LOST_IP, onIpEvent, nullptr);
  if (err != ESP_OK) {
    Serial.printf("[cell] IP event registration failed: %s\n",
                  esp_err_to_name(err));
    return false;
  }

  usbh_cdc_driver_config_t usb = {
    .task_stack_size = 4096,
    .task_priority = configMAX_PRIORITIES - 1,
    .task_coreid = 0,
    .skip_init_usb_host_driver = false,
  };
  err = usbh_cdc_driver_install(&usb);
  if (err != ESP_OK) {
    Serial.printf("[cell] USB host failed: %s\n", esp_err_to_name(err));
    return false;
  }

  usbh_modem_config_t modem = {
    .modem_id_list = modemIds,
    .at_tx_buffer_size = 256,
    .at_rx_buffer_size = 256,
    .pdp = {
      .enable = true,
      .cid = 1,
      .type = "IP",
      .apn = "globaldata",
    },
  };
  err = usbh_modem_install(&modem);
  if (err != ESP_OK) {
    Serial.printf("[cell] modem install failed: %s\n", esp_err_to_name(err));
    usbh_cdc_driver_uninstall();
    return false;
  }

  esp_netif_t *netif = usbh_modem_get_netif();
  if (netif) esp_netif_set_default_netif(netif);
  installed = true;
  Serial.println("[cell] USB host ready; waiting for SIM7670G");
  return true;
}

void cellularService() {
  if (!gotIp || announced) return;
  announced = true;
  esp_netif_ip_info_t info = {};
  esp_netif_t *netif = usbh_modem_get_netif();
  if (netif && esp_netif_get_ip_info(netif, &info) == ESP_OK) {
    Serial.printf("[cell] data up, IP " IPSTR "\n", IP2STR(&info.ip));
  } else {
    Serial.println("[cell] data up");
  }
}

bool cellularConnected() {
  return gotIp;
}

// The USB-modem prototype's session is owned by its host driver.
void cellularRestart() {}
void cellularShutdown() {}
bool cellularPoweredDown() { return true; }
bool cellularNetworkTime(int64_t &) { return false; }

int cellularRssiDbm() {
  // CSQ is reported in a modem-specific 0..31 scale. Until that query is
  // cached outside the audio loop, omit a made-up dBm value with -1.
  return -1;
}

void cellularPrintStatus() {
  Serial.printf("[cell] USB host=%s, data=%s, APN=globaldata\n",
                installed ? "ready" : "down", gotIp ? "up" : "waiting");
}

bool cellularCommand(const String &) { return false; }
#endif  // RELAY_BOARD_06
