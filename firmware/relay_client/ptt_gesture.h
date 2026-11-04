#pragma once

#include <stdint.h>

// Push-to-talk also opens Muse pairing. A press becomes speech only once held
// for PTT_TALK_HOLD_MS, so a quick tap reaches nobody (it used to earn a
// "didn't catch that"), and three quick taps open pairing. The microphone
// records from the press itself, so the hold costs no words.
static constexpr uint32_t PTT_TALK_HOLD_MS = 250;
static constexpr uint32_t PTT_TAP_WINDOW_MS = 1500;
static constexpr int PTT_PAIRING_TAPS = 3;

inline bool pttIsSpeech(uint32_t heldMs) { return heldMs >= PTT_TALK_HOLD_MS; }

class PttTaps {
 public:
  // Call on every release. True on the third tap inside the window; speech
  // in between starts the count again.
  bool release(uint32_t pressedAt, uint32_t releasedAt) {
    if (pttIsSpeech(releasedAt - pressedAt)) {
      taps_ = 0;
      return false;
    }
    if (taps_ == 0 || releasedAt - firstTapAt_ > PTT_TAP_WINDOW_MS) {
      taps_ = 0;
      firstTapAt_ = pressedAt;
    }
    if (++taps_ < PTT_PAIRING_TAPS) return false;
    taps_ = 0;
    return true;
  }

 private:
  int taps_ = 0;
  uint32_t firstTapAt_ = 0;
};
