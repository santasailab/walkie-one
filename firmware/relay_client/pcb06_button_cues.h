#pragma once

#include <atomic>
#include <stddef.h>
#include <stdint.h>

enum class Pcb06CueKind : uint8_t { Talk, Play, VolumeUp, VolumeDown };

struct Pcb06ButtonSample {
  uint32_t now = 0;
  bool pressed[4] = {}; // Talk, Play, Volume Up, Volume Down.
};

// Separate from VolumeButton: its held-button repeats still change volume,
// while feedback belongs only to a stable downstroke.
class Pcb06CueDownstroke {
 public:
  bool update(bool pressed, uint32_t now) {
    if (!initialized_) {
      initialized_ = true;
      raw_ = stable_ = pressed;
      changed_ = now;
      return false;
    }
    if (pressed != raw_) { raw_ = pressed; changed_ = now; }
    if (raw_ == stable_ || uint32_t(now - changed_) < 20) return false;
    stable_ = raw_;
    return stable_;
  }
 private:
  bool initialized_ = false, raw_ = false, stable_ = false;
  uint32_t changed_ = 0;
};

struct Pcb06CueRequest {
  Pcb06CueKind kind = Pcb06CueKind::Talk;
  uint8_t volume = 0;
  uint32_t queuedAt = 0, generation = 0;
};

// One producer (loop), one consumer (audioTask). Enqueuing never waits for an
// I2S write or the playback mutex. Mute invalidates even already-popped cues.
class Pcb06CueQueue {
 public:
  static constexpr size_t kCapacity = 16;
  static constexpr uint32_t kMaxAgeMs = 250;
  bool push(Pcb06CueKind kind, int volume, bool muted, uint32_t now) {
    if (muted || volume <= 0) return false;
    const uint32_t head = head_.load(std::memory_order_relaxed);
    if (head - tail_.load(std::memory_order_acquire) >= kCapacity) return false;
    entries_[head % kCapacity] = {kind, uint8_t(volume > 10 ? 10 : volume), now,
                                 generation_.load(std::memory_order_acquire)};
    head_.store(head + 1, std::memory_order_release);
    return true;
  }
  bool pop(Pcb06CueRequest &request, uint32_t now) {
    for (size_t n = 0; n < kCapacity; ++n) {
      const uint32_t tail = tail_.load(std::memory_order_relaxed);
      if (tail == head_.load(std::memory_order_acquire)) return false;
      request = entries_[tail % kCapacity];
      tail_.store(tail + 1, std::memory_order_release);
      if (valid(request) && uint32_t(now - request.queuedAt) <= kMaxAgeMs) return true;
    }
    return false;
  }
  void cancel() { generation_.fetch_add(1, std::memory_order_acq_rel); }
  bool valid(const Pcb06CueRequest &request) const {
    return request.generation == generation_.load(std::memory_order_acquire);
  }
 private:
  Pcb06CueRequest entries_[kCapacity];
  std::atomic<uint32_t> head_{0}, tail_{0}, generation_{0};
};

// Audio-task-owned cursor. It never reads or alters the MP3 reply or mic buffer.
// Original 24k PCM is duplicated to both stereo slots and two 48k frames.
class Pcb06CuePlayer {
 public:
  static constexpr size_t kChunkSamples = 240;
  static constexpr size_t kDrainSamples = 1200; // 50ms > the 40ms TX DMA queue.
  void start(const Pcb06CueRequest &request, const int16_t *pcm, size_t count,
             size_t drainSamples = kDrainSamples) {
    request_ = request; pcm_ = pcm; count_ = count; position_ = 0;
    drain_ = drainSamples; active_ = true; silent_ = false;
    preparedBytes_ = sentBytes_ = preparedSamples_ = 0;
  }
  bool active() const { return active_; }
  bool silent() const { return silent_; }
  const Pcb06CueRequest &request() const { return request_; }
  void cancel() {
    if (!active_ || silent_) return;
    silent_ = true; position_ = count_; drain_ = kDrainSamples;
    preparedBytes_ = sentBytes_ = preparedSamples_ = 0;
  }
  void clear() { active_ = false; preparedBytes_ = sentBytes_ = 0; }
  const uint8_t *data() {
    if (!preparedBytes_ && active_) {
      const bool sound = position_ < count_;
      const size_t remaining = sound ? count_ - position_ : drain_;
      preparedSamples_ = remaining < kChunkSamples ? remaining : kChunkSamples;
      for (size_t i = 0; i < preparedSamples_; ++i) {
        // Initial cue calibration is unity at master volume 10, independent
        // of the reply-only gain control. No new clipping or waveform EQ.
        const int16_t sample = sound ? int32_t(pcm_[position_ + i]) * request_.volume / 10 : 0;
        for (size_t slot = 0; slot < 4; ++slot) buffer_[i * 4 + slot] = sample;
      }
      preparedBytes_ = preparedSamples_ * 4 * sizeof(int16_t);
      sentBytes_ = 0;
    }
    return reinterpret_cast<const uint8_t *>(buffer_) + sentBytes_;
  }
  size_t bytes() const { return preparedBytes_ - sentBytes_; }
  bool commit(size_t written) {
    if (written > bytes()) return false;
    sentBytes_ += written;
    if (preparedBytes_ && sentBytes_ == preparedBytes_) {
      if (position_ < count_) position_ += preparedSamples_;
      else drain_ -= preparedSamples_;
      preparedBytes_ = sentBytes_ = 0;
      if (position_ == count_ && !drain_) active_ = false;
    }
    return true;
  }
 private:
  Pcb06CueRequest request_;
  const int16_t *pcm_ = nullptr;
  size_t count_ = 0, position_ = 0, drain_ = 0;
  size_t preparedBytes_ = 0, sentBytes_ = 0, preparedSamples_ = 0;
  bool active_ = false, silent_ = false;
  int16_t buffer_[kChunkSamples * 4] = {};
};
