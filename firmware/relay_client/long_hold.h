#pragma once
#include <stdint.h>

// Soft off: hold Play this long to switch off, and the shorter time to switch
// back on. Off is long enough not to be accidental. Neither hold deletes replies.
constexpr uint32_t kSoftOffHoldMs = 5000;
constexpr uint32_t kSoftOnHoldMs = 2000;
// A running loop gets this long to close its socket before switching off
// restarts the firmware anyway.
constexpr uint32_t kSoftOffGraceMs = 1000;

// Switching off or on restarts the firmware. This marker, kept in RTC memory
// across a software restart, selects the switched-off runtime. A power-on reset
// leaves that memory undefined, so it always starts on.
constexpr uint32_t kSoftOffMarker = 0x57314F46;  // "W1OF"
inline bool bootsOff(uint32_t marker, bool powerOnReset) {
  return !powerOnReset && marker == kSoftOffMarker;
}

// Fires once when a button has been held for holdMs; release re-arms it.
// Subtraction is safe across millis() wrap. A button already held when this
// starts (the hold that restarted the firmware) must be released first.
class LongHold {
 public:
  explicit LongHold(bool heldAtStart = false) : armed_(!heldAtStart) {}

  bool update(bool pressed, uint32_t nowMs, uint32_t holdMs) {
    if (!pressed) {
      armed_ = true;
      holding_ = false;
      return false;
    }
    if (!armed_) return false;
    if (!holding_) {
      holding_ = true;
      since_ = nowMs;
    }
    if (uint32_t(nowMs - since_) < holdMs) return false;
    armed_ = false;
    return true;
  }

 private:
  bool armed_, holding_ = false;
  uint32_t since_ = 0;
};
