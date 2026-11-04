#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string.h>

// One physical Talk press owns one bounded append-only PCM buffer. The caller
// allocates it in PSRAM and protects EVERY method (including getters) with the
// same mutex. Copy a network chunk under that lock, then release it BEFORE any
// socket operation so the microphone worker can keep collecting samples.
//
// This class neither enables the microphone nor sends protocol frames. The
// caller must stop the microphone on physical release/disconnect/deadline,
// independently of network progress. In particular, stop() receives the real
// release/deadline time, not the time a blocked network loop eventually resumes.
class Pcb06CaptureSpool {
 public:
  static constexpr uint32_t kSampleRate = 24000;
  static constexpr uint32_t kMaxCaptureMs = 60000;
  static constexpr size_t kMaxSamples = kSampleRate * kMaxCaptureMs / 1000;
  static constexpr uint32_t kSpeechHoldMs = 250;

  enum class State { Idle, Capturing, Draining, Complete, Discarded, Failed };
  enum class Failure { None, Overflow, Disconnected, Transport, Capture };

  struct Chunk {
    size_t samples = 0;
    uint32_t generation = 0;
    size_t offset = 0;
  };

  Pcb06CaptureSpool(int16_t *storage, size_t capacity)
      : storage_(storage), capacity_(capacity) {}

  bool begin(uint32_t now, bool physicallyHeld, bool connectionReady) {
    if (!physicallyHeld || !connectionReady || !storage_ || !capacity_
        || active()) return false;
    eraseStored();
    written_ = consumed_ = 0;
    startedAt_ = now;
    stoppedAfterMs_ = 0;
    started_ = false;
    failure_ = Failure::None;
    state_ = State::Capturing;
    if (++generation_ == 0) ++generation_;
    return true;
  }

  // All-or-nothing append: an overflow invalidates the entire turn instead of
  // silently dropping old/new words. The worker clips a final block to remaining()
  // and stops at kMaxCaptureMs/kMaxSamples before attempting another append.
  bool append(const int16_t *samples, size_t count) {
    if (state_ != State::Capturing) return false;
    if (count > remaining() || (count && !samples)) {
      abort(Failure::Overflow);
      return false;
    }
    if (count) memcpy(storage_ + written_, samples, count * sizeof(int16_t));
    written_ += count;
    return true;
  }

  void stop(uint32_t now) {
    if (state_ != State::Capturing) return;
    stoppedAfterMs_ = now - startedAt_;
    if (stoppedAfterMs_ < kSpeechHoldMs || written_ == 0) {
      eraseStored();
      consumed_ = written_;
      state_ = State::Discarded;
      return;
    }
    state_ = State::Draining;
  }

  bool needsStart(uint32_t now) const {
    return active() && !started_ && written_ != 0
        && durationMs(now) >= kSpeechHoldMs;
  }

  // Call only AFTER talk.start was successfully written on this connection.
  bool markStarted(uint32_t now) {
    if (!needsStart(now)) return false;
    started_ = true;
    return true;
  }

  Chunk copyPending(int16_t *destination, size_t maximum) const {
    if (!active() || !started_ || !destination || !maximum) return {};
    size_t count = pending();
    if (count > maximum) count = maximum;
    if (!count) return {};
    memcpy(destination, storage_ + consumed_, count * sizeof(int16_t));
    return {count, generation_, consumed_};
  }

  // Call only AFTER the complete copied WebSocket frame was successfully sent.
  // A repeated/stale acknowledgment must not consume another chunk/turn.
  bool acknowledge(const Chunk &chunk) {
    if (!active() || !started_ || !chunk.samples
        || chunk.generation != generation_ || chunk.offset != consumed_
        || chunk.samples > pending()) return false;
    memset(storage_ + consumed_, 0, chunk.samples * sizeof(int16_t));
    consumed_ += chunk.samples;
    return true;
  }

  bool needsEnd() const {
    return state_ == State::Draining && started_ && pending() == 0;
  }

  // Call only AFTER talk.end was successfully written. Keep the Device busy
  // until this point so a new press cannot be interleaved with the old upload.
  bool finish() {
    if (!needsEnd()) return false;
    state_ = State::Complete;
    return true;
  }

  // Stop the hardware first, then clear buffered speech. A transport/capture
  // failure after talk.start requires closing that connection, not talk.end:
  // otherwise the server could execute an incomplete spoken request.
  void abort(Failure failure) {
    if (state_ == State::Failed) return;  // Cleanup must not hide the first cause.
    eraseStored();
    consumed_ = written_;
    failure_ = failure;
    state_ = State::Failed;
  }

  bool active() const {
    return state_ == State::Capturing || state_ == State::Draining;
  }
  State state() const { return state_; }
  Failure failure() const { return failure_; }
  uint32_t generation() const { return generation_; }
  bool started() const { return started_; }
  size_t captured() const { return written_; }
  size_t pending() const { return written_ - consumed_; }
  size_t remaining() const {
    const size_t limit = capacity_ < kMaxSamples ? capacity_ : kMaxSamples;
    return written_ < limit ? limit - written_ : 0;
  }
  uint32_t durationMs(uint32_t now) const {
    return state_ == State::Capturing ? now - startedAt_ : stoppedAfterMs_;
  }

 private:
  void eraseStored() {
    if (written_) memset(storage_, 0, written_ * sizeof(int16_t));
  }

  int16_t *storage_;
  size_t capacity_;
  size_t written_ = 0, consumed_ = 0;
  uint32_t generation_ = 0, startedAt_ = 0, stoppedAfterMs_ = 0;
  State state_ = State::Idle;
  Failure failure_ = Failure::None;
  bool started_ = false;
};
