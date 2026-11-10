#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <initializer_list>

namespace cellular_policy {

constexpr size_t kMaxApn = 100;
constexpr uint32_t kConfigMagic = 0x43454c4c;
constexpr uint32_t kMaxRetryDelayMs = 300000;
// SIM7672G power-off gesture: PWRKEY held at least 2.5 s, then the module
// detaches and shuts down before the supply may be removed. The settle time
// is a placeholder until measured on hardware (no STATUS pin on PCB06).
constexpr uint32_t kPowerOffHoldMs = 2600;
constexpr uint32_t kPowerOffSettleMs = 3000;
// How long a restart waits for the worker to finish that: notice (a bounded
// modem command may be in flight), hang up, the gesture, the settle.
constexpr uint32_t kPowerOffBudgetMs = 10000;
static_assert(kPowerOffBudgetMs >= kPowerOffHoldMs + kPowerOffSettleMs + 2000,
              "restart budget must cover the power-off sequence");

enum class AtResult { Pending, Ok, Error };

// Only complete, whitespace-trimmed terminal lines are results. Echoed commands
// and payload text containing "OK"/"ERROR" must not finish a transaction.
inline AtResult terminalResult(const char *response, size_t length) {
  size_t start = 0;
  for (size_t end = 0; end < length; ++end) {
    if (response[end] != '\r' && response[end] != '\n') continue;
    size_t first = start, last = end;
    while (first < last && (response[first] == ' ' || response[first] == '\t')) ++first;
    while (last > first && (response[last - 1] == ' ' || response[last - 1] == '\t')) --last;
    const size_t size = last - first;
    if (size == 2 && std::memcmp(response + first, "OK", 2) == 0) return AtResult::Ok;
    if ((size == 5 && std::memcmp(response + first, "ERROR", 5) == 0) ||
        (size >= 11 && (std::memcmp(response + first, "+CME ERROR:", 11) == 0 ||
                        std::memcmp(response + first, "+CMS ERROR:", 11) == 0)))
      return AtResult::Error;
    start = end + 1;
  }
  return AtResult::Pending;
}

// One NVS value keeps the enable flag and APN together across interrupted writes.
struct Config {
  uint32_t magic;
  uint8_t version;
  uint8_t enabled;
  char apn[kMaxApn + 1];
  uint8_t reserved;
};
static_assert(sizeof(Config) == 108, "NVS cellular configuration layout changed");

inline Config emptyConfig() {
  Config result{};
  result.magic = kConfigMagic;
  result.version = 1;
  return result;
}

inline bool validApn(const char *apn) {
  if (!apn || !*apn) return false;
  for (size_t i = 0; i <= kMaxApn; ++i) {
    const unsigned char c = static_cast<unsigned char>(apn[i]);
    if (!c) return i <= kMaxApn;
    if (i == kMaxApn || !((c >= 'a' && c <= 'z') ||
        (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
        c == '.' || c == '-')) return false;
  }
  return false;
}

inline bool validConfig(const Config &value) {
  if (value.magic != kConfigMagic || value.version != 1 || value.enabled > 1 ||
      value.reserved != 0 || !std::memchr(value.apn, 0, sizeof(value.apn))) return false;
  return value.apn[0] ? validApn(value.apn) : value.enabled == 0;
}

inline bool advertisesBaud(const char *response, int baud) {
  const char *line = response ? std::strstr(response, "+IPR:") : nullptr;
  if (!line) return false;
  line += 5;
  while (*line && *line != '\r' && *line != '\n') {
    if (*line >= '0' && *line <= '9') {
      char *end = nullptr;
      const long value = std::strtol(line, &end, 10);
      if (value == baud) return true;
      line = end;
    } else ++line;
  }
  return false;
}

inline int preferredBaud(const char *response, int ceiling = 921600) {
  for (const int baud : {921600, 460800, 230400, 115200}) {
    if (baud <= ceiling && advertisesBaud(response, baud)) return baud;
  }
  return 0;
}

inline int slowerBaud(int baud) {
  if (baud > 460800) return 460800;
  if (baud > 230400) return 230400;
  if (baud > 115200) return 115200;
  return 0;
}

inline int taggedNumber(const char *response, const char *tag) {
  const char *value = response ? std::strstr(response, tag) : nullptr;
  if (!value) return -1;
  value += std::strlen(tag);
  while (*value == ' ' || *value == '\t') ++value;
  if (*value < '0' || *value > '9') return -1;
  char *end = nullptr;
  const long number = std::strtol(value, &end, 10);
  if (*end && *end != ',' && *end != ' ' && *end != '\r' && *end != '\n') return -1;
  return number >= 0 && number <= 1000000 ? static_cast<int>(number) : -1;
}

inline int signalDbm(const char *response) {
  const int csq = taggedNumber(response, "+CSQ:");
  return csq >= 0 && csq <= 31 ? -113 + csq * 2 : -1;
}

// AT+CCLK? answers +CCLK: "yy/MM/dd,hh:mm:ss+zz". The SIM7672G reports
// UTC digits with the local zone appended, as measured below. Returns UTC
// seconds since 1970, or 0 for malformed or unset 1970/1980 clock defaults.
inline int64_t networkTimeUtc(const char *response) {
  const char *p = response ? std::strstr(response, "+CCLK: \"") : nullptr;
  if (!p) return 0;
  p += 8;
  const auto pair = [](const char *text) {
    return text[0] >= '0' && text[0] <= '9' && text[1] >= '0' && text[1] <= '9'
        ? (text[0] - '0') * 10 + (text[1] - '0') : -1;
  };
  int field[6];
  for (int i = 0; i < 6; ++i) {
    if ((field[i] = pair(p)) < 0) return 0;
    p += 2;
    if (i < 5 && *p++ != "//,::"[i]) return 0;
  }
  if (*p != '+' && *p != '-') return 0;
  const bool west = *p++ == '-';
  const int quarters = pair(p);
  if (quarters < 0 || p[2] != '"' || quarters > (west ? 48 : 56)) return 0;
  const int year = 2000 + field[0], month = field[1], day = field[2];
  const bool leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
  static constexpr int monthDays[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
  // Two-digit years from 70 up are the modem's 19xx defaults, never 20xx.
  if (field[0] < 24 || field[0] > 69 || month < 1 || month > 12 || day < 1 ||
      day > monthDays[month - 1] + (month == 2 && leap) ||
      field[3] > 23 || field[4] > 59 || field[5] > 59) return 0;
  // Days from 1970-01-01, proleptic Gregorian.
  const int y = year - (month <= 2), m = month + (month > 2 ? -3 : 9);
  const int64_t days = 365LL * y + y / 4 - y / 100 + y / 400 + (153 * m + 2) / 5 + day - 1 - 719468;
  // The SIM7672G keeps the digits in UTC and appends the network's local zone
  // (measured on PCB06: 19:16 UTC reported as "19:16:24-32" on a US Pacific
  // network), unlike 3GPP's local-time form. The zone is checked, not applied.
  return days * 86400 + field[3] * 3600 + field[4] * 60 + field[5];
}

// Wait after consecutive failed attempts, without an attempt limit.
// Out of coverage, the delay increases to a maximum of five minutes.
inline uint32_t retryDelayMs(unsigned attempts) {
  if (attempts <= 1) return 5000;
  if (attempts == 2) return 15000;
  uint32_t wait = 30000;
  for (unsigned i = 3; i < attempts && wait < kMaxRetryDelayMs; ++i) wait *= 2;
  return wait < kMaxRetryDelayMs ? wait : kMaxRetryDelayMs;
}

inline bool expired(uint32_t now, uint32_t start, uint32_t duration) {
  return static_cast<uint32_t>(now - start) >= duration;
}

}  // namespace cellular_policy
