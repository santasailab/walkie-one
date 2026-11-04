#include "power_status.h"
#include <assert.h>
#include <string.h>

int main() {
  PowerStatus power;
  assert(power.state() == nullptr);
  assert(!power.update(true, true, 0));
  assert(!power.update(true, true, 999));
  assert(power.update(true, true, 1000));
  assert(strcmp(power.state(), "charging") == 0);
  assert(power.usb() && power.charging());
  assert(power.needsReport());
  power.reported();
  assert(!power.needsReport());
  assert(!power.update(true, true, 2000));
  // A short glitch never changes the reported state.
  power.update(false, false, 2100);
  power.update(true, true, 2400);
  assert(!power.update(true, true, 3400));
  assert(!power.needsReport());
  // USB absent always rules out charging, even if CHRG remains asserted.
  power.update(false, true, 4000);
  assert(power.update(false, true, 5000));
  assert(strcmp(power.state(), "on_battery") == 0);
  power.update(true, false, 6000);
  assert(power.update(true, false, 7000));
  assert(strcmp(power.state(), "plugged_in") == 0); // never "full"
  assert(power.usb() && !power.charging());
  assert(power.count() == 3);
  assert(power.observation(1).at == 5000 && !power.observation(1).usb);
  // Keep a bounded chronological history across USB detachment and wraparound.
  for (unsigned i = 0; i < 12; ++i) {
    power.update(i % 2, false, 8000 + i * 2000);
    power.update(i % 2, false, 9000 + i * 2000);
  }
  assert(power.count() == 8);
  assert(power.observation(7).at == 31000);
  PowerStatus rollover;
  rollover.update(true, true, UINT32_MAX - 499);
  assert(!rollover.update(true, true, 499));
  assert(rollover.update(true, true, 500));
}
