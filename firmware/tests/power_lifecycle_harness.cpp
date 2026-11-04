// Runs the sketch's real powerTask() and restartInto() across simulated
// restarts. setup() chooses the runtime exactly as boot() does below.
#include <cassert>
#include <cstdint>
#include <vector>

#include "long_hold.h"

constexpr int LOW = 0, HIGH = 1;
struct Restart {};
struct End {};
static uint32_t now = 0, end = 0;
static std::vector<uint32_t> held;  // press, release, press, ... times
static std::vector<uint32_t> restarts;
static uint32_t softOffMarker = kSoftOffMarker;  // undefined at power-on
static bool asleep = false;
static volatile bool offRequested = false;
static volatile bool batteryEmpty = false, switchOnRefused = false;
static struct { int play = 8; } buttons;
static struct {
  template <class... T> void printf(const char *, T...) {}
} Serial;

int digitalRead(int) {
  bool pressed = false;
  for (uint32_t edge : held) pressed ^= now >= edge;
  return pressed ? LOW : HIGH;
}
uint32_t millis() { return now; }
#define pdMS_TO_TICKS(ms) (ms)
void vTaskDelay(uint32_t ms) {
  now += ms;
  if (now >= end) throw End{};
}
void esp_restart() {
  restarts.push_back(now);
  throw Restart{};
}

// SKETCH_FUNCTIONS

static void boot(bool powerOn) {
  asleep = bootsOff(softOffMarker, powerOn);
  if (!asleep) softOffMarker = 0;
  offRequested = false;
}

// Runs from power-on until `until`, restarting the task like the chip would.
static void run(std::vector<uint32_t> edges, uint32_t until) {
  now = 0;
  end = until;
  held = edges;
  restarts.clear();
  softOffMarker = kSoftOffMarker;
  boot(true);
  for (;;) {
    try {
      powerTask(nullptr);
    } catch (Restart &) {
      boot(false);
    } catch (End &) {
      return;
    }
  }
}

int main() {
  // Power-on starts on even when RTC memory happens to hold the marker.
  run({}, 30000);
  assert(!asleep && restarts.empty() && softOffMarker == 0);

  // Short holds do nothing.
  run({1000, 5990, 7000, 8000}, 30000);
  assert(!asleep && restarts.empty());

  // Off: a stalled loop never answers offRequested, so the grace runs out.
  // Holding on through the restart does not wake it.
  run({1000, 20000}, 30000);
  assert(asleep && softOffMarker == kSoftOffMarker);
  assert(restarts.size() == 1);
  assert(restarts[0] >= 1000 + kSoftOffHoldMs + kSoftOffGraceMs);
  assert(restarts[0] <= 1000 + kSoftOffHoldMs + kSoftOffGraceMs + 20);

  // Off then on, with a too-short wake hold first. Holding on after waking
  // does not switch off again.
  run({1000, 7000, 8000, 9900, 11000, 30000}, 40000);
  assert(!asleep && softOffMarker == 0 && restarts.size() == 2);
  assert(restarts[1] >= 11000 + kSoftOnHoldMs && restarts[1] <= 11000 + kSoftOnHoldMs + 20);

  // Repeated cycles each restart once per hold.
  std::vector<uint32_t> cycles;
  for (uint32_t t = 1000; t < 100000; t += 10000) {
    const bool off = (cycles.size() / 2) % 2 == 0;
    cycles.push_back(t);
    cycles.push_back(t + (off ? 7000 : 3000));
  }
  run(cycles, 100000);
  assert(restarts.size() == cycles.size() / 2 && asleep == (restarts.size() % 2 == 1));

  // An empty battery refuses switching on: no restart, and loop() is told.
  batteryEmpty = true;
  switchOnRefused = false;
  run({1000, 7000, 8000, 11000}, 20000);
  assert(asleep && restarts.size() == 1 && switchOnRefused);
  batteryEmpty = false;

  // A watchdog restart while off keeps the marker; one while on does not.
  softOffMarker = kSoftOffMarker;
  boot(false);
  assert(asleep);
  softOffMarker = 0;
  boot(false);
  assert(!asleep);
  return 0;
}
