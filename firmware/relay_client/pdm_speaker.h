#pragma once

#include <cstdint>

// Repaired unit workaround, measured 2026-10-05: its hand-wired MAX98357 does
// not decode I2S sample values; its output follows how often the DATA line is
// high. So the DATA line is driven as a 1-bit DAC instead: a second-order
// sigma-delta bitstream. At 48 kHz 16-bit stereo framing, each 24 kHz input
// sample becomes 64 bits = four 16-bit slot words, linearly interpolated from
// the previous sample. Silence is a 50% density bitstream.
//
// The amp is only linear within about +/-0.25 of mid-level and saturates past
// it, which is what turned loud syllables into static. Each reply is scaled so
// its own peak lands at kLevel: the owner's pick from a five-way listening A/B.
class PdmSpeaker {
 public:
  static constexpr int kWordsPerSample = 4;
  static constexpr float kCeiling = 0.9f;  // second-order loop stays stable below ~0.9
  static constexpr float kLevel = 0.20f;   // reply peak at volume 10
  static constexpr int32_t kMinPeak = 4096;  // near-silent replies are not boosted past 8x

  void encode(int32_t raw, int32_t peak, int volume, bool muted, uint16_t *out) {
    if (peak < kMinPeak) peak = kMinPeak;
    float target = muted ? 0.0f : raw * kLevel * volume / (10.0f * peak);
    if (target > kCeiling) target = kCeiling;
    if (target < -kCeiling) target = -kCeiling;
    const float step = (target - last_) / (kWordsPerSample * 16);
    float x = last_;
    for (int w = 0; w < kWordsPerSample; ++w) {
      uint16_t word = 0;
      for (int b = 0; b < 16; ++b) {
        x += step;
        i1_ += x - q_;
        i2_ += i1_ - q_;
        q_ = i2_ >= 0 ? 1.0f : -1.0f;
        word = static_cast<uint16_t>((word << 1) | (q_ > 0));
      }
      out[w] = word;
    }
    last_ = target;
  }

 private:
  float last_ = 0, i1_ = 0, i2_ = 0, q_ = 0;
};
