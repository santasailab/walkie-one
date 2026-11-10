#pragma once
#include <stdint.h>

// Keeps the chosen volume across restarts (soft off and on is a restart)
// without a flash write per click: a changed volume is written once it has
// rested for kSettleMs. The caller owns the storage and reports each write.
class VolumeStore {
 public:
  static constexpr uint32_t kSettleMs = 3000;

  // A stored value outside 0-10 (never written, or corrupt) is ignored.
  static int restore(int stored, int fallback) {
    return stored >= 0 && stored <= 10 ? stored : fallback;
  }
  void loaded(int volume) { saved_ = seen_ = volume; }
  // True while a changed volume has rested long enough to be written.
  bool due(int volume, uint32_t now) {
    if (volume != seen_) {
      seen_ = volume;
      changedAt_ = now;
    }
    return seen_ != saved_ && uint32_t(now - changedAt_) >= kSettleMs;
  }
  // The outcome of that write. A failure is tried again after another rest.
  void written(bool ok, uint32_t now) {
    if (ok) saved_ = seen_;
    else changedAt_ = now;
  }

 private:
  int saved_ = -1, seen_ = -1;
  uint32_t changedAt_ = 0;
};
