#include <assert.h>

#include "battery_watch.h"

int main() {
  BatteryWatch watch;
  assert(!watch.low() && !watch.empty() && watch.volumeCeiling() == 10);

  // A full discharge on battery: one warning at 15 %, one at 5 %, off at 3 %.
  assert(watch.sample(80, 3.95f, false, false) == BatteryAlert::None);
  assert(watch.sample(19, 3.76f, false, false) == BatteryAlert::None);
  assert(watch.volumeCeiling() == kBatteryQuietVolume && !watch.low());
  assert(watch.sample(15, 3.74f, false, false) == BatteryAlert::Low);
  assert(watch.low());
  assert(watch.sample(14, 3.74f, false, false) == BatteryAlert::None);  // once
  assert(watch.sample(5, 3.70f, false, false) == BatteryAlert::VeryLow);
  assert(watch.sample(4, 3.70f, false, false) == BatteryAlert::None);
  watch.sample(3, 3.66f, false, false);
  assert(!watch.empty());  // one sample is not enough
  watch.sample(3, 3.66f, false, false);
  assert(watch.empty());

  // Plugging in clears empty and low, and silences warnings.
  assert(watch.sample(3, 3.66f, true, false) == BatteryAlert::None);
  assert(!watch.empty() && !watch.low() && watch.volumeCeiling() == 10);
  // Unplugged again before re-arming: no repeat warning.
  assert(watch.sample(14, 3.74f, false, false) == BatteryAlert::None);
  // Charged above 20 % and the re-arm voltage: the next discharge warns again.
  watch.sample(40, 3.85f, true, false);
  assert(watch.sample(15, 3.74f, false, false) == BatteryAlert::Low);

  // Voltage alone trips the thresholds when the gauge reads high.
  BatteryWatch sagging;
  assert(sagging.sample(40, kBatteryLowVolts - 0.01f, false, false) == BatteryAlert::Low);
  assert(sagging.sample(40, kBatteryLowVolts - 0.01f, false, false) == BatteryAlert::None);
  sagging.sample(40, kBatteryEmptyVolts - 0.01f, false, false);
  sagging.sample(40, kBatteryEmptyVolts - 0.01f, false, false);
  assert(sagging.empty());

  // Sag while talking or playing never counts toward empty.
  BatteryWatch loaded;
  loaded.sample(50, 3.90f, false, false);
  loaded.sample(50, 3.40f, false, true);
  loaded.sample(50, 3.40f, false, true);
  assert(!loaded.empty() && !loaded.low() && loaded.volumeCeiling() == 10);

  // A failed read forgets the empty count.
  BatteryWatch flaky;
  flaky.sample(2, 3.50f, false, false);
  flaky.unknown();
  flaky.sample(2, 3.50f, false, false);
  assert(!flaky.empty() && !flaky.full());

  assert(batteryEmptyNow(3, 4.0f) && batteryEmptyNow(50, kBatteryEmptyVolts - 0.01f));
  assert(!batteryEmptyNow(4, kBatteryEmptyVolts));
  BatteryWatch full;
  full.sample(95, 4.1f, true, false);
  assert(full.full());
  return 0;
}
