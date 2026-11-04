#pragma once

#include <stdint.h>

namespace reconnect_policy {

static constexpr uint32_t kMinimumMs = 1000;
static constexpr uint32_t kMaximumMs = 60000;
static constexpr uint32_t kStableRedialMs = 30000;
static constexpr uint32_t kNetworkReturnMs = 5000;
static constexpr uint32_t kAuthorizationMs = 900000;
static constexpr uint32_t kRepeatedAuthorizationMs = 6 * 60 * 60 * 1000;
static constexpr uint8_t kAuthorizationFailures = 5;
static constexpr uint32_t kStableMs = 30000;

inline uint32_t fullJitterDelay(uint32_t randomValue, uint32_t ceilingMs) {
  if (ceilingMs == UINT32_MAX) return randomValue;
  return randomValue % (ceilingMs + 1);
}

inline uint32_t raisedCeiling(uint32_t ceilingMs) {
  if (ceilingMs >= kMaximumMs) return ceilingMs;
  return ceilingMs * 2 < kMaximumMs ? ceilingMs * 2 : kMaximumMs;
}

inline bool heldStable(uint32_t nowMs, uint32_t connectedSinceMs) {
  return nowMs - connectedSinceMs >= kStableMs;
}

// Clock and entropy come from the caller so the same policy runs on every board
// and in host tests. Unsigned elapsed time handles millis rollover and long
// offline periods without interpreting an expired deadline as a future one.
class Policy {
 public:
  bool due(uint32_t now) const {
    return !scheduled_ || uint32_t(now - scheduledAt_) >= waitMs_;
  }
  void attempting() { attempted_ = true; scheduled_ = false; }
  void connected(uint32_t now) {
    connected_ = true;
    stable_ = false;
    connectedSince_ = now;
  }
  void service(uint32_t now) {
    if (connected_ && !stable_ && heldStable(now, connectedSince_)) {
      stable_ = true;
      ceilingMs_ = kStableRedialMs;
      authorizationFailures_ = 0;
    }
  }
  uint32_t retry(uint32_t now, uint32_t randomValue, int closeCode = 0) {
    service(now);  // Also covers a drop exactly at the stable boundary.
    connected_ = false;
    if (closeCode == 4401) {
      if (authorizationFailures_ < kAuthorizationFailures) ++authorizationFailures_;
      ceilingMs_ = authorizationFailures_ >= kAuthorizationFailures
          ? kRepeatedAuthorizationMs : kAuthorizationMs;
    } else if (closeCode == 4409) {
      authorizationFailures_ = 0;
      ceilingMs_ = kAuthorizationMs;
    }
    const uint32_t wait = schedule(now, fullJitterDelay(randomValue, ceilingMs_));
    ceilingMs_ = raisedCeiling(ceilingMs_);
    return wait;
  }
  uint32_t networkAvailable(uint32_t now, uint32_t randomValue) {
    // Only the very first dial after boot (or explicit reprovisioning) is fast.
    uint32_t wait = attempted_ ? fullJitterDelay(randomValue, kNetworkReturnMs) : 0;
    // A carrier/IP change cannot repair a refused credential or replacement.
    // Preserve a longer pending authorization wait, including across rollover.
    if (ceilingMs_ >= kAuthorizationMs && scheduled_ && !due(now)) {
      const uint32_t remaining = waitMs_ - uint32_t(now - scheduledAt_);
      if (remaining > wait) wait = remaining;
    }
    return schedule(now, wait);
  }

 private:
  uint32_t schedule(uint32_t now, uint32_t wait) {
    scheduledAt_ = now;
    waitMs_ = wait;
    scheduled_ = true;
    return wait;
  }
  uint32_t ceilingMs_ = kMinimumMs;
  uint32_t scheduledAt_ = 0;
  uint32_t waitMs_ = 0;
  uint32_t connectedSince_ = 0;
  uint8_t authorizationFailures_ = 0;
  bool scheduled_ = false;
  bool attempted_ = false;
  bool connected_ = false;
  bool stable_ = false;
};

}  // namespace reconnect_policy
