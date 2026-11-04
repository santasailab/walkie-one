#pragma once

#include <cmath>
#include <cstddef>
#include <cstdint>

// Repaired unit's 2026-10-05 audition2/sample4: 24 kHz EQ, rounded PCM16,
// linear 24->32 kHz conversion, then volume. No normalization or limiter.
// This reduces reported background hiss; it does not fix the analog rasp.
class RepairedSpeaker {
 public:
  static constexpr size_t kBlockSamples = 240;
  void reset() { hp_ = highPass(); shelf_ = highShelf(); }

  // Non-final chunks contain a multiple of three source samples. If available,
  // input[count] is lookahead, read through a copy of the filter state so it is
  // consumed exactly once by the next chunk. Output is interleaved stereo.
  size_t render(const int16_t *input, size_t count, bool hasNext,
                int gainEighths, int volume, int cap, bool muted,
                int16_t *output) {
    if (count == 0 || count > kBlockSamples || (hasNext && count % 3)) return 0;
    int16_t filtered[kBlockSamples + 1];
    for (size_t i = 0; i < count; ++i) filtered[i] = filter(input[i]);
    RepairedSpeaker ahead = *this;
    filtered[count] = hasNext ? ahead.filter(input[count]) : filtered[count - 1];
    const size_t frames = count * 4 / 3;
    for (size_t j = 0; j < frames; ++j) {
      const size_t phase = j * 3;
      const size_t index = phase / 4;
      int32_t value = filtered[index];
      value += (int32_t(filtered[index + 1]) - value) * int32_t(phase % 4) / 4;
      int32_t s = ((value * gainEighths) / 8) * volume * cap / 1000;
      if (muted) s = 0;
      if (s > 32767) s = 32767;
      if (s < -32768) s = -32768;
      output[j * 2] = output[j * 2 + 1] = static_cast<int16_t>(s);
    }
    return frames;
  }

 private:
  struct Biquad {
    float b0,b1,b2,a1,a2;
    float x1=0,x2=0,y1=0,y2=0;
    float process(float x) {
      const float y=b0*x+b1*x1+b2*x2-a1*y1-a2*y2;
      x2=x1; x1=x; y2=y1; y1=y; return y;
    }
  };
  static Biquad highPass() {
    return {.967222573627f,-1.93444514725f,.967222573627f,-1.93337081293f,.935519481573f};
  }
  static Biquad highShelf() {
    return {.771244454975f,-.720049199466f,.254550401581f,-1.08016230955f,.385907966639f};
  }
  int16_t filter(int16_t raw) {
    float y=shelf_.process(hp_.process(raw));
    if (y>32767) y=32767;
    if (y< -32768) y=-32768;
    return static_cast<int16_t>(std::lround(y));
  }
  Biquad hp_ = highPass(), shelf_ = highShelf();
};
