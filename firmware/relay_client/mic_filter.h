#pragma once

#include <stdint.h>

// Two cascaded one-pole stages at roughly 60 Hz form a quiet, integer-only
// high-pass with a combined knee near 100 Hz at the Device's 24 kHz rate.
// Speech above the knee is preserved while handling rumble before the PCM16
// clamp, where it can otherwise consume headroom permanently.
class MicHighPass {
 public:
  void reset() {
    estimate1_ = 0;
    estimate2_ = 0;
  }

  int32_t process(int32_t sample) {
    const int32_t first = stage(sample, estimate1_);
    return stage(first, estimate2_);
  }

 private:
  static int32_t stage(int32_t sample, int32_t &estimate) {
    estimate += (sample - estimate) >> 6;
    return sample - estimate;
  }

  int32_t estimate1_ = 0;
  int32_t estimate2_ = 0;
};
