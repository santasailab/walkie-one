#include <cassert>
#include <cstdint>

#include "clock_seed.h"

int main() {
  const int64_t build = 1793000000;        // this image's build stamp
  const int64_t real = build + 200 * 86400; // the day it is actually running

  // No clock and no network time: the build stamp, as before.
  assert(clockSeed(0, build, 0, false) == build);
  assert(clockSeed(86400, build, 0, false) == build);
  // No clock, and the network knows the time: use it.
  assert(clockSeed(0, build, real, false) == real);

  // A clock still running from the build stamp months later is behind a
  // renewed certificate's validity; the network's time moves it forward.
  assert(clockSeed(build + 3600, build, real, false) == real);
  // A synchronized clock agrees with the network and is left alone.
  assert(clockSeed(real, build, real, false) == 0);
  assert(clockSeed(real, build, real + kClockStepS, false) == 0);
  assert(clockSeed(real, build, real + kClockStepS + 1, false) == real + kClockStepS + 1);
  // The clock is never moved backward: SNTP owns small corrections.
  assert(clockSeed(real + 86400, build, real, false) == 0);
  assert(clockSeed(real, build, 0, false) == 0);

  // Once SNTP has answered it owns the clock. A modem clock that is wrong
  // but plausible must not drag it forward again before every dial.
  assert(clockSeed(real, build, real + 5 * 86400, true) == 0);
  assert(clockSeed(0, build, real, true) == real);  // but never leave it unset
  // Nor is a network time years past this image's life believable.
  assert(clockSeed(0, build, build + kBuildHorizonS + 1, false) == build);
  assert(clockSeed(0, build, build + kBuildHorizonS, false) == build + kBuildHorizonS);

  // A network time from before this image existed is not the time.
  assert(clockSeed(0, build, build - kBuildSlackS - 1, false) == build);
  assert(clockSeed(0, build, build - kBuildSlackS, false) == build - kBuildSlackS);
  assert(clockSeed(build + 3600, build, 315964800, false) == 0);
}
