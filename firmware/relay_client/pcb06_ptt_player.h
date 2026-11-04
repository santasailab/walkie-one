#pragma once

#include <stddef.h>
#include <stdint.h>

// Audio-task-owned local feedback, separate from microphone and reply state.
class Pcb06PttPlayer {
 public:
  enum class Phase { Idle, Open, Bed, Close, Drain, Flush };
  static constexpr size_t kChunkSamples = 240, kFadeSamples = 240;
  static constexpr size_t kDrainSamples = 1200;
  // Approved listening calibration: -24 dB from the asset, -18 dB from the old bed.
  // Opening and closing cues retain their independently selected levels.
  static constexpr int kBedGainNumerator = 1, kBedGainDenominator = 16;
  Pcb06PttPlayer(const int16_t *open, size_t openCount,
                const int16_t *close, size_t closeCount,
                const int16_t *bed, size_t bedCount)
      : open_(open), close_(close), bed_(bed), openCount_(openCount),
        closeCount_(closeCount), bedCount_(bedCount) {}

  void observe(bool down, bool silent, uint32_t now) {
    now_ = now;
    if (!initialized_) { initialized_ = true; changedAt_ = now; }
    if (down != rawDown_) {
      rawDown_ = down; changedAt_ = now;
      if (!down) {
        releasePending_ = true;
        if (phase_ == Phase::Flush && afterFlush_ == AfterFlush::Bed)
          afterFlush_ = AfterFlush::Idle;
        if (!silent && (phase_ == Phase::Open || phase_ == Phase::Bed)) beginClose();
      } else if (phase_ == Phase::Close || phase_ == Phase::Drain) {
        // Old close samples may already be in DMA. Gate the amp and flush
        // them before this press can start a new sound; never delay the mic.
        beginFlush(stableDown_ ? AfterFlush::Bed : AfterFlush::Idle);
      }
    }
    if (rawDown_ != stableDown_ && uint32_t(now - changedAt_) >= 20) {
      stableDown_ = rawDown_;
      if (stableDown_) {
        ++generation_; releasePending_ = false;
        if (!silent) {
          if (phase_ == Phase::Flush) afterFlush_ = AfterFlush::Open;
          else beginOpen();
        }
      }
    }
    if (silent && !silent_) {
      ++generation_;
      if (active()) beginFlush(AfterFlush::Idle);
    } else if (!silent && silent_ && stableDown_ && rawDown_) {
      // A held line resumes its bed, never an old opening or closing event.
      ++generation_;
      if (phase_ == Phase::Flush) afterFlush_ = AfterFlush::Bed;
      else beginBed();
    }
    silent_ = silent;
    if (silent_) afterFlush_ = AfterFlush::Idle;
  }

  bool active() const { return phase_ != Phase::Idle || preparedBytes_ != 0; }
  bool flushing() const { return phase_ == Phase::Flush; }
  bool ampAllowed() const { return audibleEpisode_ && !silent_ && phase_ != Phase::Flush; }
  bool held() const { return stableDown_ && rawDown_; }
  Phase phase() const { return phase_; }
  uint32_t generation() const { return generation_; }
  uint32_t flushEpoch() const { return flushEpoch_; }
  size_t bedPosition() const { return bedPosition_; }
  bool overdue(uint32_t now) const {
    const size_t samples = phase_ == Phase::Open ? openCount_ : closeCount_;
    return (phase_ == Phase::Open || phase_ == Phase::Close)
        && uint32_t(now - phaseAt_) > samples / 24 + 1000;
  }
  void fail() {
    ++generation_; phase_ = Phase::Idle; audibleEpisode_ = false;
    afterFlush_ = AfterFlush::Idle; discard();
  }

  const uint8_t *data(int volume) {
    if (!preparedBytes_) {
      if (phase_ == Phase::Flush && remaining_ == 0) {
        const auto next = afterFlush_; afterFlush_ = AfterFlush::Idle;
        if (next == AfterFlush::Open && !silent_) beginOpen();
        else if (next == AfterFlush::Bed && held() && !silent_) beginBed();
        else { phase_ = Phase::Idle; audibleEpisode_ = false; }
      }
      if (phase_ == Phase::Idle) return nullptr;
      for (size_t i = 0; i < kChunkSamples; ++i) {
        const int16_t sample = int32_t(nextSample()) * (volume < 0 ? 0 : volume > 10 ? 10 : volume) / 10;
        for (size_t slot = 0; slot < 4; ++slot) buffer_[i * 4 + slot] = sample;
      }
      preparedBytes_ = sizeof(buffer_); sentBytes_ = 0;
    }
    return reinterpret_cast<const uint8_t *>(buffer_) + sentBytes_;
  }
  size_t bytes() const { return preparedBytes_ - sentBytes_; }
  bool commit(size_t written) {
    if (written > bytes()) return false;
    sentBytes_ += written;
    if (preparedBytes_ && sentBytes_ == preparedBytes_) {
      discard();
      if (phase_ == Phase::Idle) audibleEpisode_ = false;
    }
    return true;
  }

 private:
  enum class AfterFlush { Idle, Open, Bed };
  void discard() { preparedBytes_ = sentBytes_ = 0; }
  void beginOpen() {
    phase_ = Phase::Open; position_ = 0; phaseAt_ = now_;
    audibleEpisode_ = true; discard();
  }
  void beginBed() {
    phase_ = Phase::Bed; bedPosition_ = 0; fadeIn_ = 0; phaseAt_ = now_;
    audibleEpisode_ = true; discard();
  }
  int16_t bedSample() {
    const int32_t sample = int32_t(bed_[bedPosition_]) * kBedGainNumerator / kBedGainDenominator;
    bedPosition_ = (bedPosition_ + 1) % bedCount_;
    if (fadeIn_ < kFadeSamples) return sample * int32_t(++fadeIn_) / int32_t(kFadeSamples);
    return sample;
  }
  void beginClose() {
    // A just-accepted press still gets its first block, even if the finger
    // released while old DMA was flushing. Do not gate that opening on held.
    if (phase_ == Phase::Open && position_ == 0) return;
    for (size_t i = 0; i < kFadeSamples; ++i) {
      if (phase_ == Phase::Bed) fadeTail_[i] = bedSample();
      else fadeTail_[i] = position_ < openCount_ ? open_[position_++] : 0;
    }
    phase_ = Phase::Close; position_ = 0; phaseAt_ = now_; discard();
  }
  void beginFlush(AfterFlush next) {
    ++generation_; ++flushEpoch_; phase_ = Phase::Flush; remaining_ = kDrainSamples;
    afterFlush_ = next; audibleEpisode_ = false; discard();
  }
  int16_t nextSample() {
    if (phase_ == Phase::Open) {
      if (releasePending_ && position_ >= kFadeSamples) beginClose();
      else if (position_ < openCount_) return open_[position_++];
      else if (held()) beginBed();
      else beginClose();
    }
    if (phase_ == Phase::Bed) return bedSample();
    if (phase_ == Phase::Close) {
      if (position_ < closeCount_) {
        int32_t sample = close_[position_];
        if (position_ < kFadeSamples)
          sample = (sample * int32_t(position_) + int32_t(fadeTail_[position_])
                    * int32_t(kFadeSamples - position_)) / int32_t(kFadeSamples);
        ++position_; return sample;
      }
      phase_ = Phase::Drain; remaining_ = kDrainSamples;
    }
    if (phase_ == Phase::Drain || phase_ == Phase::Flush) {
      if (remaining_) --remaining_;
      if (!remaining_ && phase_ == Phase::Drain) phase_ = Phase::Idle;
    }
    return 0;
  }

  const int16_t *open_, *close_, *bed_;
  size_t openCount_, closeCount_, bedCount_;
  Phase phase_ = Phase::Idle;
  AfterFlush afterFlush_ = AfterFlush::Idle;
  bool initialized_ = false, rawDown_ = false, stableDown_ = false;
  bool silent_ = false, releasePending_ = false, audibleEpisode_ = false;
  uint32_t changedAt_ = 0, now_ = 0, phaseAt_ = 0, generation_ = 0, flushEpoch_ = 0;
  size_t position_ = 0, bedPosition_ = 0, fadeIn_ = 0, remaining_ = 0;
  size_t preparedBytes_ = 0, sentBytes_ = 0;
  int16_t fadeTail_[kFadeSamples] = {}, buffer_[kChunkSamples * 4] = {};
};
