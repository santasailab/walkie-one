#pragma once
#include <stddef.h>
#include <stdint.h>

// Independent, bounded instances; callers own task synchronization and storage.
// Input is MPEG-2 Layer III mono 24 kHz MP3; output is PCM16 mono 24 kHz.
// No audio is ever written to flash.
class Mp3Playback {
 public:
  static constexpr size_t kMaxBytes = 1024 * 1024;  // about 175 s at 48 kbit/s
  Mp3Playback();
  ~Mp3Playback();
  bool available() const { return state != nullptr; }
  // Fully decode/validate before advertising a complete span. yield() lets the
  // caller keep its watchdog alive while validating long replies.
  bool prepare(const uint8_t *data, size_t bytes, size_t &samples, int32_t &peak,
               void (*yield)() = nullptr);
  void rewind();
  bool read(size_t sample, int16_t *out, size_t count);
 private:
  void *state = nullptr;
};
