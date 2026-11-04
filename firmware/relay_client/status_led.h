#pragma once
#include <stdint.h>

// The single status light:
//   white  = you: talking (solid), pairing/checking (blinking), volume (flash).
//            The 06 board's red die gets 1 k against 470 R for green and
//            blue, so its white reads as aqua; the owner guide says aqua.
//   blue   = assistant working (slow blink), Walkie One speaking (solid)
//   green  = a reply is waiting (slow blink), fully charged (steady)
//   orange = charging (steady)
//   red    = something needs fixing: pairing failed (solid, 3 s),
//            no assistant (slow blink), no connection (fast blink)
// Solid means happening now; blinking means waiting; faster means a problem.
// Battery lights fill idle time; connection success adds no light.

// Channel levels 0-255, scaled by kLedBrightness on the way out.
struct Rgb {
  uint8_t red, green, blue;
  bool operator==(const Rgb &other) const {
    return red == other.red && green == other.green && blue == other.blue;
  }
};

enum class PairingLight { Off, Open, Paired, Failed };
enum class BatteryLight { None, Charging, Charged };

struct StatusInputs {
  bool asleep = false;
  BatteryLight battery = BatteryLight::None;
  PairingLight pairing = PairingLight::Off;
  bool recording = false;
  bool volumeChanged = false;  // within the brief feedback window
  bool playing = false;
  bool replyWaiting = false;   // complete staged span the digest counts as missed
  bool cloudConnected = false;
  uint32_t offlineMs = 0;      // how long the cloud has been unreachable
  bool assistantReady = false;
  bool assistantWorking = false;
  bool connectionInProgress = false;
};

// Tune on a 06 board: charging orange must read apart from error red.
constexpr uint8_t kOrangeGreen = 24;
constexpr Rgb kOff{0, 0, 0}, kWhite{255, 255, 255}, kBlue{0, 0, 255},
    kGreen{0, 255, 0}, kRed{255, 0, 0},
    kOrange{255, kOrangeGreen, 0};

// 8-bit PWM, shared by status, boot and diagnostics.
#ifdef RELAY_BOARD_06
constexpr uint8_t kLedBrightness = 255;  // Full drive on PCB06.
#else
constexpr uint8_t kLedBrightness = 20;
#endif
constexpr uint8_t ledDuty(uint8_t level, bool commonAnode) {
  return commonAnode ? 255 - level * kLedBrightness / 255
                     : level * kLedBrightness / 255;
}

// Boot and brief reconnects are normal; only a lasting outage turns red.
constexpr uint32_t kOfflineGraceMs = 10000;
constexpr uint32_t kSlowBlinkMs = 1000, kBlinkMs = 500, kFastBlinkMs = 250;

constexpr Rgb scaled(Rgb color, uint8_t level) {
  return {uint8_t(color.red * level / 255), uint8_t(color.green * level / 255),
          uint8_t(color.blue * level / 255)};
}

inline Rgb blinking(Rgb color, uint32_t nowMs, uint32_t halfPeriodMs) {
  return (nowMs / halfPeriodMs) & 1 ? color : kOff;
}

inline Rgb batteryLed(BatteryLight battery) {
  switch (battery) {
    case BatteryLight::Charging: return kOrange;
    case BatteryLight::Charged: return kGreen;
    case BatteryLight::None: break;
  }
  return kOff;
}

inline Rgb statusLed(const StatusInputs &in, uint32_t nowMs) {
  // Switched off, only the charger is worth showing.
  if (in.asleep) return batteryLed(in.battery);
  switch (in.pairing) {
    case PairingLight::Paired:
      if (!in.assistantReady || in.connectionInProgress)
        return blinking(kWhite, nowMs, kBlinkMs);
      break;
    case PairingLight::Failed: return kRed;
    case PairingLight::Open: return blinking(kWhite, nowMs, kBlinkMs);
    case PairingLight::Off: break;
  }
  if (in.recording || in.volumeChanged) return kWhite;
  if (in.playing) return kBlue;
  // A waiting reply outranks connection problems: an unclaimed Device hears
  // its spoken-claim answer before it has an assistant.
  if (in.replyWaiting) return blinking(kGreen, nowMs, kSlowBlinkMs);
  if (!in.cloudConnected) {
    if (in.offlineMs >= kOfflineGraceMs) return blinking(kRed, nowMs, kFastBlinkMs);
  } else if (in.connectionInProgress) {
    return blinking(kWhite, nowMs, kBlinkMs);
  } else if (!in.assistantReady) {
    return blinking(kRed, nowMs, kSlowBlinkMs);
  } else if (in.assistantWorking) {
    return blinking(kBlue, nowMs, kSlowBlinkMs);
  }
  return batteryLed(in.battery);
}
