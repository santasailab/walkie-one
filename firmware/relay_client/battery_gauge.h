#pragma once
#include <stdint.h>

struct BatteryReading {
  float volts;
  float percent;
  int roundedPercent() const {
    const int rounded = int(percent + 0.5f);
    return rounded > 100 ? 100 : rounded;
  }
};

// MAX17048 VCELL (78.125 uV/LSB) and SOC (1/256 percent/LSB).
// Read failures and implausible cell voltage mean unknown, never zero percent.
// Templated so the real I2C transaction can also be exercised on the host.
template <typename Bus>
bool readBatteryGauge(Bus &bus, BatteryReading &result) {
  uint16_t words[2] = {};
  for (int i = 0; i < 2; ++i) {
    bus.beginTransmission(0x36);
    bus.write(i ? 0x04 : 0x02);
    if (bus.endTransmission(false) != 0 ||
        bus.requestFrom(uint8_t(0x36), uint8_t(2)) != 2) return false;
    const uint16_t high = bus.read();
    const uint16_t low = bus.read();
    words[i] = (high << 8) | low;
  }
  const float volts = words[0] * 78.125e-6f;
  if (volts < 2.5f || volts > 4.5f || words[1] == 0xffff) return false;
  result = {volts, words[1] / 256.0f};
  return true;
}
