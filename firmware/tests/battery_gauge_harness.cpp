#include "battery_gauge.h"
#include <cassert>
struct Bus {
  uint16_t voltage = 51200, soc = 82 * 256 + 179;
  int ack = 0, count = 2, reg = 0, byte = 0;
  void beginTransmission(int address) { assert(address == 0x36); }
  void write(int value) { reg = value; byte = 0; }
  int endTransmission(bool stop) { assert(!stop); return ack; }
  int requestFrom(uint8_t address, uint8_t size) { assert(address == 0x36 && size == 2); return count; }
  int read() { const auto word = reg == 2 ? voltage : soc; return byte++ ? word & 255 : word >> 8; }
};
int main() {
  Bus bus; BatteryReading result = {};
  assert(readBatteryGauge(bus, result));
  assert(result.volts > 3.999f && result.volts < 4.001f);
  assert(result.roundedPercent() == 83);
  bus.soc = 0; assert(readBatteryGauge(bus, result) && result.roundedPercent() == 0);
  bus.soc = 101 * 256; assert(readBatteryGauge(bus, result) && result.roundedPercent() == 100);
  bus.soc = 0xffff; assert(!readBatteryGauge(bus, result));
  bus.soc = 80 * 256; bus.ack = 2; assert(!readBatteryGauge(bus, result));
  bus.ack = 0; bus.count = 1; assert(!readBatteryGauge(bus, result));
  bus.count = 2; bus.voltage = 0; assert(!readBatteryGauge(bus, result));
  bus.voltage = 0xffff; assert(!readBatteryGauge(bus, result));
}
