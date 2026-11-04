#pragma once
#include <stdint.h>

// Independent debounce and hold-repeat; subtraction is safe across millis wrap.
class VolumeButton {
 public:
  bool update(bool pressed, uint32_t now) {
    if (pressed != raw_) { raw_ = pressed; changed_ = now; }
    if (raw_ != stable_ && uint32_t(now - changed_) >= 20) {
      stable_ = raw_;
      if (stable_) { repeated_ = false; emitted_ = now; return true; }
    }
    if (stable_ && raw_ && uint32_t(now - emitted_) >= (repeated_ ? 150U : 500U)) {
      repeated_ = true;
      emitted_ = now;
      return true;
    }
    return false;
  }
 private:
  bool raw_ = false, stable_ = false, repeated_ = false;
  uint32_t changed_ = 0, emitted_ = 0;
};
