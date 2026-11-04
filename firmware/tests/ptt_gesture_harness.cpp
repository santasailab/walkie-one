#include <assert.h>

#include "ptt_gesture.h"

int main() {
  assert(!pttIsSpeech(PTT_TALK_HOLD_MS - 1));
  assert(pttIsSpeech(PTT_TALK_HOLD_MS));

  // Three quick taps open pairing, once.
  PttTaps taps;
  assert(!taps.release(1000, 1100));
  assert(!taps.release(1300, 1400));
  assert(taps.release(1600, 1700));
  assert(!taps.release(1900, 2000));

  // Speech in between starts the count again.
  PttTaps spoken;
  assert(!spoken.release(0, 100));
  assert(!spoken.release(200, 300));
  assert(!spoken.release(400, 400 + PTT_TALK_HOLD_MS));
  assert(!spoken.release(1000, 1100));
  assert(!spoken.release(1200, 1300));
  assert(spoken.release(1400, 1500));

  // Taps spread past the window do not add up.
  PttTaps slow;
  assert(!slow.release(0, 100));
  assert(!slow.release(800, 900));
  assert(!slow.release(PTT_TAP_WINDOW_MS + 100, PTT_TAP_WINDOW_MS + 200));
  assert(!slow.release(PTT_TAP_WINDOW_MS + 300, PTT_TAP_WINDOW_MS + 400));
  assert(slow.release(PTT_TAP_WINDOW_MS + 500, PTT_TAP_WINDOW_MS + 600));

  // millis() wraps after 49 days; unsigned differences still hold.
  PttTaps wrap;
  assert(!wrap.release(0xFFFFFF00u, 0xFFFFFF50u));
  assert(!wrap.release(0xFFFFFFA0u, 0x00000010u));
  assert(wrap.release(0x00000080u, 0x00000100u));
  return 0;
}
