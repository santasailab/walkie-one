#include <assert.h>

#include "long_hold.h"

int main() {
  LongHold hold;
  assert(!hold.update(false, 0, kSoftOffHoldMs));
  // Fires exactly at the threshold, once per hold.
  assert(!hold.update(true, 1000, kSoftOffHoldMs));
  assert(!hold.update(true, 1000 + kSoftOffHoldMs - 1, kSoftOffHoldMs));
  assert(hold.update(true, 1000 + kSoftOffHoldMs, kSoftOffHoldMs));
  assert(!hold.update(true, 1000 + 3 * kSoftOffHoldMs, kSoftOffHoldMs));
  assert(!hold.update(false, 40000, kSoftOffHoldMs));

  // A hold shorter than the power threshold never fires.
  assert(!hold.update(true, 50000, kSoftOffHoldMs));
  assert(!hold.update(true, 51500, kSoftOffHoldMs));
  assert(!hold.update(false, 51600, kSoftOffHoldMs));

  // Switching back on uses the shorter hold.
  assert(!hold.update(true, 60000, kSoftOnHoldMs));
  assert(hold.update(true, 60000 + kSoftOnHoldMs, kSoftOnHoldMs));

  // A hold already down at start must be released before it can fire.
  LongHold boot(true);
  assert(!boot.update(true, 0, kSoftOnHoldMs));
  assert(!boot.update(true, 10 * kSoftOnHoldMs, kSoftOnHoldMs));
  assert(!boot.update(false, 10 * kSoftOnHoldMs + 1, kSoftOnHoldMs));
  assert(!boot.update(true, 30000, kSoftOnHoldMs));
  assert(boot.update(true, 30000 + kSoftOnHoldMs, kSoftOnHoldMs));

  // Only a software restart with the exact marker starts switched off.
  assert(bootsOff(kSoftOffMarker, false));
  assert(!bootsOff(kSoftOffMarker, true));
  assert(!bootsOff(0, false));

  // millis() wrap.
  LongHold wrap;
  assert(!wrap.update(true, 0xFFFFFFFFu - 999, kSoftOnHoldMs));
  assert(wrap.update(true, kSoftOnHoldMs - 1000, kSoftOnHoldMs));
  return 0;
}
