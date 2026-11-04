#include "src/audio/mp3_codec.h"
#include "playback_state.h"
#include <assert.h>
#include <algorithm>
#include <cstdio>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>

static std::vector<uint8_t> load(const char *path) {
  std::ifstream file(path, std::ios::binary);
  return std::vector<uint8_t>((std::istreambuf_iterator<char>(file)), {});
}

// `--check file.mp3` validates a file and prints its decoded sample count.
static int check(const char *path) {
  auto data = load(path);
  Mp3Playback playback;
  size_t samples = 0;
  int32_t peak = 0;
  if (!playback.prepare(data.data(), data.size(), samples, peak)) return 1;
  std::vector<int16_t> pcm(samples);
  if (!playback.read(0, pcm.data(), samples)) return 1;
  printf("%zu\n", samples);
  return 0;
}

// `reply.mp3 out.pcm` exercises staging/retry rules and writes the decode.
int main(int argc, char **argv) {
  if (argc == 3 && std::string(argv[1]) == "--check") return check(argv[2]);
  assert(argc == 3);
  const auto encoded = load(argv[1]);
  Mp3Playback a, b;
  size_t samples = 0, samplesB = 0;
  int32_t peak = 0, peakB = 0;
  assert(a.available() && b.available());
  assert(a.prepare(encoded.data(), encoded.size(), samples, peak));
  assert(b.prepare(encoded.data(), encoded.size(), samplesB, peakB));
  assert(samples == samplesB && samples > 24000);
  assert(peak > 1000 && peak <= 32768 && peak == peakB);
  std::vector<int16_t> decoded(samples), second(samples);
  for (size_t i = 0; i < samples; i += 240) {
    const size_t n = std::min(size_t(240), samples - i);
    // Read one extra interpolation sample, then resume at the consumed index.
    int16_t block[241];
    assert(a.read(i, block, std::min(n + 1, samples - i)));
    assert(b.read(i, second.data() + i, n));
    std::copy(block, block + n, decoded.data() + i);
  }
  assert(decoded == second);
  // Retry from any previously consumed offset, including a frame boundary.
  int16_t retry[241];
  assert(a.read(570, retry, 241));
  assert(std::equal(retry, retry + 241, decoded.data() + 570));
  std::ofstream(argv[2], std::ios::binary).write((char *)decoded.data(), decoded.size() * 2);

  // The exact production cursor retains a long response through many pauses.
  // Includes interpolation lookahead and a pause after decoding but before
  // submitting a block, which must be decoded again on resume without skipping.
  PlaybackState state;
  state.stage(samplesB);
  assert(state.play());
  std::vector<int16_t> resumed;
  b.rewind();
  while (state.played < state.samples) {
    const size_t offset = state.played;
    const size_t n = std::min(size_t(240), size_t(state.samples - offset));
    int16_t block[241];
    assert(b.read(offset, block, std::min(n + 1, size_t(state.samples - offset))));
    const uint32_t beforePause = state.generation;
    assert(state.pause());
    assert(state.staged && !state.ready && !state.finished);
    assert(state.played == offset);
    state.end(beforePause, true); // an obsolete block cannot acknowledge the span
    assert(!state.finished);
    assert(state.play());
    assert(!state.current(beforePause));
    assert(b.read(state.played, block, n));
    resumed.insert(resumed.end(), block, block + n);
    state.played += n;
  }
  state.end(state.generation, true);
  assert(state.finished && !state.pause());
  assert(resumed == decoded); // every sample once, no restart and no skipped word
  state.clear(); // reconnect/new span loses the volatile cursor
  assert(state.played == 0 && !state.staged && !state.finished && !state.play());
  state.stage(samplesB);
  assert(state.play());
  state.end(state.generation, false);
  assert(state.failed && !state.finished && !state.pause());

  auto invalid = encoded;
  invalid.pop_back();
  assert(!a.prepare(invalid.data(), invalid.size(), samples, peak));
  invalid = encoded; invalid[2] = 0; // free format/unsupported rate
  assert(!a.prepare(invalid.data(), invalid.size(), samples, peak));
  const uint8_t junk[10] = {'I','D','3',4,0,0,127,127,127,127};
  assert(!a.prepare(junk, sizeof(junk), samples, peak));
  assert(!a.prepare(encoded.data(), 1024 * 1024 + 1, samples, peak));
  assert(!a.prepare(encoded.data(), 0, samples, peak));
  puts("MP3 decode, lookahead, retry, independent instances and invalid input passed");
}
