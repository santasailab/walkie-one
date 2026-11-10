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

// A run of brownout restarts, counted in RTC memory, which survives them. The
// battery gauge can read healthy while the cell sags under the modem's
// transmit bursts; the empty-battery check then passes and, without this, the
// Device would restart into the same brownout for ever.
constexpr uint32_t kBrownoutMagic = 0x42524F57;         // "BROW"
constexpr uint8_t kBrownoutLimit = 3;                    // restarts in a row
constexpr uint32_t kBrownoutSettledMs = 5UL * 60 * 1000;  // running this long ends a run

struct BrownoutRecord {
  uint32_t magic;
  uint8_t count;
};

// Once per boot. True when this boot must stay switched off. RTC memory is
// undefined after power-on, and any other kind of restart ends the run, so
// switching the Device on again is a fresh attempt.
inline bool brownoutStaysOff(BrownoutRecord &record, BootCause cause) {
  if (record.magic != kBrownoutMagic || cause != BootCause::Brownout) {
    record.magic = kBrownoutMagic;
    record.count = 0;
    return false;
  }
  if (record.count < UINT8_MAX) ++record.count;
  return record.count >= kBrownoutLimit;
}

// The Device has run well for kBrownoutSettledMs: an isolated brownout later
// starts a new run.
inline void brownoutSettled(BrownoutRecord &record) { record.count = 0; }
