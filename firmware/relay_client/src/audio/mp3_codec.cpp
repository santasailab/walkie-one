#include "mp3_codec.h"
#include <stdlib.h>
#include <string.h>
#define MINIMP3_IMPLEMENTATION
#define MINIMP3_ONLY_MP3
#define MINIMP3_NO_SIMD
#include "minimp3/minimp3.h"
#include "codec_alloc.h"

namespace {
// Bounds a low-bitrate file that fits kMaxBytes but would play for many minutes.
constexpr size_t MaxSamples = 24000 * 180;
struct Decoder {
  mp3dec_t codec;
  mp3dec_scratch_t scratch;
  int16_t pcm[MINIMP3_MAX_SAMPLES_PER_FRAME];
  const uint8_t *data;
  size_t begin, end, cursor, frameStart, count;

  void rewind() {
    mp3dec_init(&codec);
    cursor = begin;
    frameStart = count = 0;
  }
  bool next() {
    if (cursor + 4 > end) return false;
    const uint8_t *p = data + cursor;
    static const int rates[] = {0,8,16,24,32,40,48,56,64,80,96,112,128,144,160,0};
    const int bitrate = rates[p[2] >> 4];
    if (p[0] != 255 || (p[1] & 254) != 242 || (p[2] & 12) != 4 ||
        (p[3] >> 6) != 3 || !bitrate) return false;
    const size_t bytes = 72000 * bitrate / 24000 + ((p[2] >> 1) & 1);
    if (bytes > end - cursor) return false;
    mp3dec_frame_info_t info{};
    const int n = mp3dec_decode_frame(&codec, p, int(end - cursor), pcm, &info, &scratch);
    if (n <= 0 || info.frame_bytes != int(bytes) || info.channels != 1 ||
        info.hz != 24000 || info.layer != 3) return false;
    cursor += bytes;
    frameStart += count;
    count = size_t(n);
    return frameStart + count <= MaxSamples;
  }
};
}

Mp3Playback::Mp3Playback() { state = calloc(1, sizeof(Decoder)); }
Mp3Playback::~Mp3Playback() { free(state); }
void Mp3Playback::rewind() {
  if (state) static_cast<Decoder *>(state)->rewind();
}
bool Mp3Playback::prepare(const uint8_t *data, size_t bytes, size_t &samples,
                          int32_t &peak, void (*yield)()) {
  if (!state || !bytes || bytes > kMaxBytes) return false;
  auto &s = *static_cast<Decoder *>(state);
  s.data = data; s.begin = 0; s.end = bytes;
  if (bytes >= 10 && !memcmp(data, "ID3", 3)) {
    for (int i = 6; i < 10; ++i) if (data[i] & 128) return false;
    s.begin = 10 + (size_t(data[6]) << 21) + (size_t(data[7]) << 14) +
              (size_t(data[8]) << 7) + data[9] + ((data[5] & 16) ? 10 : 0);
  }
  if (bytes >= 128 && !memcmp(data + bytes - 128, "TAG", 3)) s.end -= 128;
  if (s.begin >= s.end) return false;
  s.rewind(); samples = 0; peak = 0;
  while (s.cursor < s.end) {
    if (!s.next()) return false;
    samples += s.count;
    for (size_t i = 0; i < s.count; ++i) {
      const int32_t v = s.pcm[i];
      const int32_t mag = v < 0 ? -v : v;
      if (mag > peak) peak = mag;
    }
    if (yield) yield();
  }
  s.rewind();
  return samples > 0;
}
bool Mp3Playback::read(size_t offset, int16_t *out, size_t count) {
  if (!state) return false;
  auto &s = *static_cast<Decoder *>(state);
  if (offset < s.frameStart) s.rewind();
  while (count) {
    while (offset >= s.frameStart + s.count) if (!s.next()) return false;
    const size_t available = s.frameStart + s.count - offset;
    const size_t n = count < available ? count : available;
    memcpy(out, s.pcm + (offset - s.frameStart), n * sizeof(int16_t));
    offset += n; out += n; count -= n;
  }
  return true;
}
