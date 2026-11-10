#include <cassert>
#include <cstdint>

#include "battery_gauge.h"
#include "battery_watch.h"
#include "long_hold.h"

constexpr int LOW = 0, HIGH = 1, PIN_USB_PRESENT_N = 41;
static struct Bus {
  uint16_t voltage = 51200, soc = 50 * 256;  // 4.000 V, 50 %
  int ack = 0, reg = 0, byte = 0, registersRead = 0;
  int laterSoc = -1;  // what the gauge reports from the second reading on
  void beginTransmission(int) {}
  void write(int value) {
    reg = value; byte = 0;
    if (++registersRead > 2 && laterSoc >= 0) soc = uint16_t(laterSoc);
  }
  int endTransmission(bool) { return ack; }
  int requestFrom(uint8_t, uint8_t) { return 2; }
  int read() { const auto word = reg == 2 ? voltage : soc; return byte++ ? word & 255 : word >> 8; }
} Wire;
static bool usbPresent = false;
static int digitalRead(int pin) { assert(pin == PIN_USB_PRESENT_N); return usbPresent ? LOW : HIGH; }
static struct { void println(const char *) {} } Serial;
static unsigned waitedMs = 0;
static void delay(unsigned ms) { waitedMs += ms; }
static volatile bool batteryEmpty = false, switchOnRefused = false;
static uint32_t softOffMarker = 0;

// SKETCH_FUNCTIONS

static void reset(uint16_t voltage, uint16_t soc, bool usb, int ack = 0) {
  Wire.voltage = voltage; Wire.soc = soc; Wire.ack = ack;
  Wire.registersRead = 0; Wire.laterSoc = -1;
  waitedMs = 0;
  usbPresent = usb;
  batteryEmpty = switchOnRefused = false;
  softOffMarker = 0;
}

int main() {
  assert(batteryAllowsStart(true, 50, 3.90f, false));
  assert(!batteryAllowsStart(true, kBatteryEmptyPercent, 3.90f, false));
  assert(!batteryAllowsStart(true, 50, kBatteryEmptyVolts - 0.01f, false));
  assert(batteryAllowsStart(true, 0, 3.30f, true));  // on USB the charger carries it
  assert(batteryAllowsStart(false, 0, 0.0f, false));  // a dead gauge never strands a Device

  // A healthy battery starts normally.
  reset(51200, 50 * 256, false);
  assert(!board06StartsOff(false) && !batteryEmpty && !switchOnRefused && softOffMarker == 0);

  // Switched on, or restarted by a brownout, with an empty battery: this boot
  // and the next ones stay off instead of powering the modem again.
  reset(51200, 2 * 256, false);
  assert(board06StartsOff(false) && batteryEmpty && switchOnRefused);
  assert(bootsOff(softOffMarker, false));
  reset(44800, 50 * 256, false);  // 3.500 V under a gauge that still reads 50 %
  assert(board06StartsOff(false) && batteryEmpty && bootsOff(softOffMarker, false));

  // Staying off takes two empty readings, as switching off while running
  // does: one unsettled sample after a reset must not strand the Device.
  reset(51200, 2 * 256, false);
  Wire.laterSoc = 50 * 256;
  assert(!board06StartsOff(false) && !batteryEmpty && softOffMarker == 0 && waitedMs >= 250);
  reset(51200, 50 * 256, false);
  assert(!board06StartsOff(false) && waitedMs == 0);  // a healthy start is not delayed

  // On USB, or with an unreadable gauge, it starts.
  reset(51200, 2 * 256, true);
  assert(!board06StartsOff(false) && !batteryEmpty && softOffMarker == 0);
  reset(51200, 2 * 256, false, 2);
  assert(!board06StartsOff(false) && !batteryEmpty && softOffMarker == 0);

  // A boot that is already switched off stays off quietly either way, and
  // still learns whether switching on must be refused.
  reset(51200, 2 * 256, false);
  assert(board06StartsOff(true) && batteryEmpty && !switchOnRefused);
  reset(51200, 50 * 256, false);
  assert(board06StartsOff(true) && !batteryEmpty && !switchOnRefused);
}
