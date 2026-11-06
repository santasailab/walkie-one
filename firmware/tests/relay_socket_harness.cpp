#include <cassert>
#include <cstddef>
#include <cstdint>
#include <cstdarg>
#include <cstring>
#include <deque>
#include <string>

#include "socket_timeouts.h"

static const char *RELAY_CA = "trust anchor";
static int wdtResets = 0;
static void esp_task_wdt_reset() { ++wdtResets; }
static uint32_t esp_random() { return 0x5a; }
static size_t min(size_t a, size_t b) { return a < b ? a : b; }
static uint32_t fakeNow = 0;
static uint32_t millis() { return fakeNow; }
struct String {
  std::string text;
  String() = default;
  String(std::string value) : text(std::move(value)) {}
  int indexOf(const char *needle) const {
    const size_t at = text.find(needle);
    return at == std::string::npos ? -1 : int(at);
  }
  size_t length() const { return text.size(); }
  const char *c_str() const { return text.c_str(); }
  bool operator==(const char *other) const { return text == other; }
};
static struct { void printf(const char *, ...) {} void println(const char *) {} } Serial;
// One line of the peer's response and how long after the previous it arrives.
struct Arrival { uint32_t afterMs; std::string line; };

// Units as in the pinned ESP32 core: Stream::setTimeout() is the
// per-character wait of readStringUntil in MILLISECONDS and does not bound the
// socket; setConnectionTimeout() (milliseconds) bounds the TCP connect and a
// stalled TLS write; setHandshakeTimeout() takes SECONDS.
static void delay(uint32_t ms);
static struct FakeTls {
  const char *ca = nullptr;
  unsigned long streamMs = 1000, handshakeS = 120;
  uint32_t connectionMs = 3000;
  bool up = true;
  int failAt = -1, writes = 0, stops = 0;
  uint32_t writeMs = 0;
  std::deque<Arrival> arrivals;
  bool repeatLast = false;
  void stop() { up = false; ++stops; }
  bool available() { return !arrivals.empty() && arrivals.front().afterMs == 0; }
  void wait(uint32_t ms) {
    fakeNow += ms;
    if (!arrivals.empty()) arrivals.front().afterMs -= ms < arrivals.front().afterMs ? ms : arrivals.front().afterMs;
  }
  // Stream::readStringUntil: the line without its terminator, or what arrived
  // (here nothing) once streamMs passes with no further character.
  String readStringUntil(char) {
    if (arrivals.empty() || arrivals.front().afterMs > streamMs) { wait(streamMs); return String(); }
    wait(arrivals.front().afterMs);
    const Arrival next = arrivals.front();
    if (repeatLast && arrivals.size() == 1) arrivals.front().afterMs = next.afterMs ? next.afterMs : 4000;
    else arrivals.pop_front();
    return String(next.line);
  }
  void setCACert(const char *text) { ca = text; }
  void setTimeout(unsigned long milliseconds) { streamMs = milliseconds; }
  void setConnectionTimeout(uint32_t milliseconds) { connectionMs = milliseconds; }
  void setHandshakeTimeout(unsigned long seconds) { handshakeS = seconds; }
  bool connected() { return up; }
  int write(const uint8_t *, size_t size) {
    fakeNow += writeMs;
    if (writes++ == failAt) { up = false; return 0; }
    return int(size);
  }
} tls;

static void delay(uint32_t ms) { tls.wait(ms); }

// SKETCH_FUNCTIONS

static bool upgrade(std::deque<Arrival> arrivals, bool repeatLast = false) {
  tls.up = true;
  tls.arrivals = std::move(arrivals);
  tls.repeatLast = repeatLast;
  wdtResets = 0;
  return wsAwaitUpgrade();
}

int main() {
  configureRelaySocket();
  assert(tls.ca == RELAY_CA);
  // 15 here once meant 15 ms: upgrade headers split across TLS records were
  // abandoned, and the socket kept the core's 3 s default.
  assert(tls.streamMs == kUpgradeReadMs && kUpgradeReadMs >= 1000);
  assert(tls.connectionMs == kSocketTimeoutMs && kSocketTimeoutMs >= 10000);
  assert(tls.handshakeS == kTlsHandshakeS && kTlsHandshakeS == 30);
  // One stalled write must end before the loop watchdog does.
  assert(kSocketTimeoutMs < kLoopWatchdogMs);

  // A frame is a header write and 512-byte payload writes. Each one that
  // succeeds is progress, so a slow uplink cannot starve the loop watchdog.
  uint8_t payload[1300] = {};
  assert(wsSendFrame(0x2, payload, sizeof(payload)));
  assert(tls.writes == 4 && wdtResets == 4);

  // A write that fails ends the frame and is not progress.
  tls.writes = wdtResets = 0;
  tls.failAt = 2;
  assert(!wsSendFrame(0x2, payload, sizeof(payload)));
  assert(tls.writes == 3 && wdtResets == 2);

  wdtResets = 0;
  assert(!tls.connected() && !wsSendFrame(0x1, payload, 4) && wdtResets == 0);

  // Progress is not a licence to hold loop() for ever: a frame that cannot
  // cross in kFrameSendMs closes the socket, whatever each write managed.
  tls.up = true; tls.failAt = -1; tls.writes = tls.stops = wdtResets = 0;
  tls.writeMs = 14000;
  const uint32_t frameStarted = fakeNow;
  assert(!wsSendFrame(0x2, payload, sizeof(payload)));
  assert(tls.stops == 1 && !tls.connected() && tls.writes == 3);
  assert(fakeNow - frameStarted < kFrameSendMs + kSocketTimeoutMs);
  tls.writeMs = 0;

  // The upgrade response: a 101 status, then headers up to the blank line.
  const Arrival ok{50, "HTTP/1.1 101 Switching Protocols\r"};
  assert(upgrade({ok, {0, "Upgrade: websocket\r"}, {0, "Connection: Upgrade\r"}, {0, "\r"}}));
  assert(wdtResets > 0);
  assert(!upgrade({{50, "HTTP/1.1 401 Unauthorized\r"}, {0, "\r"}}));
  assert(!upgrade({}));  // nothing at all
  // Headers split across TLS records by seconds, as a slow uplink does.
  assert(upgrade({ok, {3000, "Upgrade: websocket\r"}, {3000, "\r"}}));
  // Headers that stop arriving are not the end of the headers: the bytes
  // still in flight would be read as WebSocket frames.
  uint32_t started = fakeNow;
  assert(!upgrade({ok, {0, "Upgrade: websocket\r"}}));
  assert(fakeNow - started <= kUpgradeReadMs + 100);
  // A peer that trickles header lines for ever is cut off, and the wait is
  // bounded even though every line fed the watchdog.
  started = fakeNow;
  assert(!upgrade({ok, {4000, "X-Padding: 1\r"}}, true));
  assert(fakeNow - started >= kUpgradeTotalMs);
  assert(fakeNow - started <= kUpgradeTotalMs + kUpgradeReadMs);
  static_assert(kUpgradeTotalMs + kUpgradeReadMs <= 30000, "bounded upgrade");
}
