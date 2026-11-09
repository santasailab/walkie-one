#pragma once

#include <algorithm>
#include <cassert>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <deque>
#include <functional>
#include <string>
#include <vector>

class String {
 public:
  String() = default;
  String(const char *s) : value(s) {}
  String(std::string s) : value(std::move(s)) {}
  String &operator=(const char *s) { value = s; return *this; }
  String &operator+=(char c) { value += c; return *this; }
  bool operator==(const char *s) const { return value == s; }
  bool operator!=(const char *s) const { return value != s; }
  size_t length() const { return value.size(); }
  const char *c_str() const { return value.c_str(); }
  bool startsWith(const char *s) const { return value.rfind(s, 0) == 0; }
  bool endsWith(const char *s) const {
    const size_t n = std::strlen(s);
    return value.size() >= n && value.compare(value.size() - n, n, s) == 0;
  }
  int indexOf(const char *s) const {
    const size_t p = value.find(s);
    return p == std::string::npos ? -1 : static_cast<int>(p);
  }
  String substring(size_t start) const { return String(value.substr(start)); }
 private:
  std::string value;
};

namespace fake_cell {
inline uint32_t now = 0;
inline int pins[49]{};
inline unsigned delays = 0, beginCalls = 0, cmdCalls = 0, endCalls = 0;
inline int lastBaud = 0, modemBaud = 115200;
inline std::string modemApn;
inline bool radioOnline = true, rejectProfile = false, ignoreBaudChange = false;
inline bool rawSilent = false, attached = true, ip = true, saveFails = false;
inline std::string capability = "+IPR: (115200,230400,460800)";
inline std::string log, apn;
inline std::string rawReply;
inline std::string clock = "+CCLK: \"70/01/01,00:00:12+00\"";
inline uint32_t rawDelayMs = 0;
inline std::vector<std::pair<uint32_t, int>> baudChanges;
inline std::function<void()> onData, onDelay;
inline std::vector<uint8_t> stored;
inline unsigned putCalls = 0;
}

constexpr int LOW = 0, HIGH = 1, INPUT = 0, OUTPUT = 1, SERIAL_8N1 = 0;
using portMUX_TYPE = int;
using TaskHandle_t = void *;
constexpr int portMUX_INITIALIZER_UNLOCKED = 0, pdPASS = 1;
#define portENTER_CRITICAL(lock) ((void)(lock))
#define portEXIT_CRITICAL(lock) ((void)(lock))
#define pdMS_TO_TICKS(ms) (ms)
inline uint32_t millis() { return fake_cell::now; }
inline void digitalWrite(int p, int v) { assert(p >= 0 && p < 49); fake_cell::pins[p] = v; }
inline void pinMode(int, int) {}
inline void vTaskDelay(uint32_t ms) {
  fake_cell::now += ms;
  ++fake_cell::delays;
  if (fake_cell::onDelay) fake_cell::onDelay();
}
inline int xTaskCreate(void (*)(void *), const char *, unsigned, void *, unsigned, TaskHandle_t *) { return pdPASS; }
struct SerialFake {
  void println(const char *s) { fake_cell::log += std::string(s) + '\n'; }
  void printf(const char *format, ...) {
    char buffer[1024]; va_list args; va_start(args, format);
    std::vsnprintf(buffer, sizeof(buffer), format, args); va_end(args);
    fake_cell::log += buffer;
  }
};
inline SerialFake Serial;

class HardwareSerial {
 public:
  explicit HardwareSerial(int) {}
  void setRxBufferSize(size_t) {}
  void begin(int rate, int, int, int) { baud = rate; }
  void end() { rx.clear(); command.clear(); }
  void updateBaudRate(int rate) { baud = rate; fake_cell::baudChanges.emplace_back(fake_cell::now, rate); }
  void flush() {}
  int available() const { return fake_cell::now >= readyAt ? static_cast<int>(rx.size()) : 0; }
  int read() { const char c = rx.front(); rx.pop_front(); return c; }
  void print(const char *s) {
    command += s;
    if (command.empty() || command.back() != '\r') return;
    if (!fake_cell::rawSilent && baud == fake_cell::modemBaud) {
      std::string response = "\r\n";
      if (command.rfind("AT+IPR=", 0) == 0 && command != "AT+IPR=?\r" && !fake_cell::ignoreBaudChange)
        fake_cell::modemBaud = std::stoi(command.substr(7));
      if (command == "AT+CFUN=4\r") fake_cell::radioOnline = false;
      if (command == "AT+CFUN=1\r") fake_cell::radioOnline = true;
      const std::string prefix = "AT+CGDCONT=1,\"IP\",\"";
      if (command.rfind(prefix, 0) == 0) {
        assert(!fake_cell::radioOnline);
        if (!fake_cell::rejectProfile)
          fake_cell::modemApn = command.substr(prefix.size(), command.size() - prefix.size() - 2);
      }
      if (command == "AT+CPIN?\r") response += "+CPIN: READY\r\n";
      if (command == "AT+IPR=?\r") response += fake_cell::capability + "\r\n";
      response += (command.rfind(prefix, 0) == 0 && fake_cell::rejectProfile) ? "ERROR\r\n" : "OK\r\n";
      if (!fake_cell::rawReply.empty()) response = fake_cell::rawReply;
      rx.insert(rx.end(), response.begin(), response.end());
      readyAt = fake_cell::now + fake_cell::rawDelayMs;
    }
    command.clear();
  }
 private:
  std::deque<char> rx;
  std::string command;
  int baud = 115200;
  uint32_t readyAt = 0;
};

constexpr int ESP_MODEM_MODE_COMMAND = 0, ESP_MODEM_MODE_DATA = 1;
constexpr int ESP_MODEM_FLOW_CONTROL_NONE = 0, PPP_MODEM_GENERIC = 0;
struct PPPFake {
  int currentMode = ESP_MODEM_MODE_COMMAND;
  void setResetPin(int) {}
  void sendAtBurst(bool) {}
  bool setApn(const char *s) { fake_cell::apn = s; return true; }
  bool setPins(int tx, int rx, int rts, int cts, int flow) {
    assert(tx == 17 && rx == 18 && rts == -1 && cts == -1 && flow == 0); return true;
  }
  bool begin(int, int uart, int baud) { assert(uart == 1 && baud == fake_cell::modemBaud); fake_cell::lastBaud = baud; ++fake_cell::beginCalls; return true; }
  void end() { ++fake_cell::endCalls; currentMode = ESP_MODEM_MODE_COMMAND; }
  bool setBaudrate(int rate) { fake_cell::lastBaud = rate; return true; }
  int mode() const { return currentMode; }
  bool mode(int m) { currentMode = m; if (fake_cell::onData) fake_cell::onData(); return true; }
  bool hasIP() const { return fake_cell::ip; }
  bool setDefault() { return true; }
  bool cmd(const char *s, String &out, int timeout) {
    assert(currentMode == ESP_MODEM_MODE_COMMAND && timeout <= 1000);
    ++fake_cell::cmdCalls;
    if (std::strcmp(s, "AT+CGATT?") == 0) out = (fake_cell::attached && fake_cell::radioOnline && fake_cell::modemApn == fake_cell::apn) ? "+CGATT: 1\r\nOK" : "+CGATT: 0\r\nOK";
    else if (std::strcmp(s, "AT+CSQ") == 0) out = "+CSQ: 15,99\r\nOK";
    else if (std::strcmp(s, "AT+CCLK?") == 0) out = (fake_cell::clock + "\r\nOK").c_str();
    else out = "OK";
    return true;
  }
};
inline PPPFake PPP;
struct NetworkFake { bool begin() { return true; } };
inline NetworkFake Network;

class Preferences {
 public:
  bool begin(const char *name, bool) { assert(std::strcmp(name, "cellular") == 0); return true; }
  void end() {}
  size_t getBytesLength(const char *) { return fake_cell::stored.size(); }
  size_t getBytes(const char *, void *out, size_t size) {
    size = std::min(size, fake_cell::stored.size());
    std::memcpy(out, fake_cell::stored.data(), size); return size;
  }
  size_t putBytes(const char *, const void *data, size_t size) {
    ++fake_cell::putCalls;
    if (fake_cell::saveFails) return 0;
    const auto *bytes = static_cast<const uint8_t *>(data);
    fake_cell::stored.assign(bytes, bytes + size); return size;
  }
};
