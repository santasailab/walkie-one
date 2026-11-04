#include <bitset>
#include <cmath>
#include <cstdio>

#include "pdm_speaker.h"

static int failures = 0;
#define CHECK(cond) do { if (!(cond)) { std::printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #cond); ++failures; } } while (0)

// Fraction of 1 bits over one second of a constant input.
static double density(int raw, int peak = 10000, int volume = 10, bool muted = false) {
  PdmSpeaker p;
  uint16_t words[PdmSpeaker::kWordsPerSample];
  long ones = 0, bits = 0;
  for (int i = 0; i < 24000; ++i) {
    p.encode(raw, peak, volume, muted, words);
    if (i < 2400) continue;
    for (uint16_t w : words) ones += std::bitset<16>(w).count();
    bits += 16 * PdmSpeaker::kWordsPerSample;
  }
  return double(ones) / bits;
}

int main() {
  const double L = PdmSpeaker::kLevel;
  CHECK(std::fabs(density(0) - 0.5) < 0.001);                       // silence: 50% density
  CHECK(std::fabs(density(10000) - (0.5 + 0.5 * L)) < 0.002);       // the reply peak lands at kLevel
  CHECK(std::fabs(density(-10000) - (0.5 - 0.5 * L)) < 0.002);
  CHECK(std::fabs(density(5000) - (0.5 + 0.25 * L)) < 0.002);       // linear in the sample
  CHECK(std::fabs(density(20000, 20000) - (0.5 + 0.5 * L)) < 0.002);  // a louder reply is scaled down
  CHECK(std::fabs(density(1000, 1000) - (0.5 + 0.5 * L * 1000 / PdmSpeaker::kMinPeak)) < 0.002);  // quiet one not over-boosted
  CHECK(std::fabs(density(10000, 10000, 5) - (0.5 + 0.25 * L)) < 0.002);
  CHECK(std::fabs(density(10000, 10000, 10, true) - 0.5) < 0.001);  // mute is silence, not a DC step

  // A 1 kHz tone survives: average each 64-bit sample block back to a level.
  PdmSpeaker p;
  uint16_t words[PdmSpeaker::kWordsPerSample];
  double dot = 0;
  for (int i = 0; i < 24000; ++i) {
    const double s = std::sin(2 * M_PI * 1000 * i / 24000.0);
    p.encode((int)std::lround(8000 * s), 8000, 10, false, words);
    int ones = 0;
    for (uint16_t w : words) ones += std::bitset<16>(w).count();
    if (i >= 2400) dot += (ones / 64.0 - 0.5) * 2 * std::sin(2 * M_PI * 1000 * (i - 1) / 24000.0);
  }
  CHECK(std::fabs(2 * dot / 21600 - L) < 0.03);
  return failures ? 1 : 0;
}
