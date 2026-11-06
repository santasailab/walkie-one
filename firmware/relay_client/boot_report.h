#pragma once
#include <stdint.h>

// Why this boot happened, reduced from esp_reset_reason() to what the firmware
// acts on and what a bench log needs to say.
enum class BootCause : uint8_t { PowerOn, Requested, Crash, Watchdog, Brownout, Other };

inline const char *bootCauseName(BootCause cause) {
  switch (cause) {
    case BootCause::PowerOn: return "power-on";
    case BootCause::Requested: return "requested restart";
    case BootCause::Crash: return "crash";
    case BootCause::Watchdog: return "watchdog";
    case BootCause::Brownout: return "brownout";
    case BootCause::Other: break;
  }
  return "other";
}

// The firmware restarted itself and nobody asked: like an update restart, it
// may be the middle of the night, so it comes back without the boot chirp.
inline bool bootIsUnattended(BootCause cause) {
  return cause == BootCause::Crash || cause == BootCause::Watchdog ||
         cause == BootCause::Brownout;
}
