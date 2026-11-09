#pragma once
#include <stdint.h>

// Which time to start TLS with. Certificate validity needs a clock, and SNTP
// may be blocked or late. The build stamp is only a floor: once the image is a
// few months old it falls before a renewed certificate's notBefore and every
// handshake fails. The mobile network's time, when the modem has it, is right.
constexpr int64_t kClockUnsetBeforeS = 1700000000;  // earlier means no clock yet
constexpr int64_t kClockStepS = 3600;               // smaller errors belong to SNTP
constexpr int64_t kBuildSlackS = 3 * 86400;         // build stamp has no time zone
constexpr int64_t kBuildHorizonS = 10LL * 365 * 86400;  // an image's believable life

// Returns the time to set, or 0 to leave the clock alone. `network` is 0 when
// unknown. A set clock that disagrees with the network by more than the step
// is replaced in either direction: one seeded from the build stamp runs
// behind, and one mis-seeded earlier, or drifted through soft-off, runs
// ahead; a synchronized clock does neither. Once SNTP has answered it owns a
// set clock; the modem's time may be wrong yet plausible, and must not undo
// SNTP before every dial.
inline int64_t clockSeed(int64_t current, int64_t build, int64_t network, bool sntpSynced) {
  const bool unset = current < kClockUnsetBeforeS;
  if (sntpSynced && !unset) return 0;
  const bool networkPlausible = network && network >= build - kBuildSlackS &&
                                network <= build + kBuildHorizonS;
  const int64_t error = network > current ? network - current : current - network;
  if (networkPlausible && (unset || error > kClockStepS)) return network;
  return unset ? build : 0;
}
