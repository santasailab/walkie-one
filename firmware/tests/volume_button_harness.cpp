#include <assert.h>
#include <stdint.h>

#include "volume_button.h"

int main() {
  VolumeButton button;
  assert(!button.update(true, 100));
  assert(!button.update(false, 105));
  assert(!button.update(true, 110));
  assert(!button.update(true, 129));
  assert(button.update(true, 130));
  assert(!button.update(true, 629));
  assert(button.update(true, 630));
  assert(button.update(true, 780));
  assert(!button.update(false, 781));
  assert(!button.update(false, 801));
  assert(!button.update(false, 2000));
  VolumeButton rollover;
  assert(!rollover.update(true, UINT32_MAX - 10));
  assert(rollover.update(true, 10));
}
