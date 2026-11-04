#pragma once
#include <stdint.h>

// When the 06 board warns, quiets and switches itself off, from MAX17048
// samples. The percentages are what the owner guide promises: low at 15 %,
// a second warning at 5 %, off at 3 %, volume capped below 20 %.
// ponytail: kBatteryEmptyVolts is a placeholder until first-article test 5
// measures where transmit bursts brown out the modem (todo.md, "Battery life
// on the 06 board"). Set it from that result; the other voltage floors follow.
constexpr int kBatteryLowPercent = 15, kBatteryVeryLowPercent = 5,
              kBatteryEmptyPercent = 3, kBatteryQuietPercent = 20,
              kBatteryFullPercent = 95;
constexpr float kBatteryEmptyVolts = 3.60f,
                kBatteryVeryLowVolts = kBatteryEmptyVolts + 0.05f,
                kBatteryLowVolts = kBatteryEmptyVolts + 0.10f,
                kBatteryRearmVolts = kBatteryEmptyVolts + 0.15f;
constexpr int kBatteryQuietVolume = 6;  // of 10

inline bool batteryEmptyNow(int percent, float volts) {
  return percent <= kBatteryEmptyPercent || volts < kBatteryEmptyVolts;
}
inline bool batteryLowNow(int percent, float volts) {
  return percent <= kBatteryLowPercent || volts < kBatteryLowVolts;
}

enum class BatteryAlert { None, Low, VeryLow };

// Fed one gauge sample at a time. Each warning plays once per discharge and
// re-arms only after charging clears both its percentage and voltage.
// Voltage sags while talking or playing (`loaded`), so those samples keep the
// last resting voltage. Switching off waits for two empty samples in a row.
class BatteryWatch {
 public:
  BatteryAlert sample(int percent, float volts, bool usb, bool loaded) {
    if (!loaded || !known_) restVolts_ = volts;
    known_ = true;
    usb_ = usb;
    percent_ = percent;
    if (percent > kBatteryQuietPercent && restVolts_ >= kBatteryRearmVolts)
      warnedLow_ = warnedVeryLow_ = false;
    emptySamples_ = !usb && batteryEmptyNow(percent, restVolts_) ? emptySamples_ + 1 : 0;
    if (usb) return BatteryAlert::None;
    if (!warnedVeryLow_ && (percent <= kBatteryVeryLowPercent || restVolts_ < kBatteryVeryLowVolts)) {
      warnedVeryLow_ = warnedLow_ = true;
      return BatteryAlert::VeryLow;
    }
    if (!warnedLow_ && batteryLowNow(percent, restVolts_)) {
      warnedLow_ = true;
      return BatteryAlert::Low;
    }
    return BatteryAlert::None;
  }
  void unknown() { known_ = false; emptySamples_ = 0; }

  bool low() const { return known_ && !usb_ && batteryLowNow(percent_, restVolts_); }
  bool empty() const { return emptySamples_ >= 2; }
  bool full() const { return known_ && percent_ >= kBatteryFullPercent; }
  int volumeCeiling() const {
    return known_ && !usb_ && (percent_ < kBatteryQuietPercent || restVolts_ < kBatteryLowVolts)
               ? kBatteryQuietVolume : 10;
  }

 private:
  bool known_ = false, usb_ = false, warnedLow_ = false, warnedVeryLow_ = false;
  int percent_ = 0, emptySamples_ = 0;
  float restVolts_ = 0;
};
