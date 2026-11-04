"""Exercise the PCB06 pin profile and extracted runtime boundaries on the host."""

import pathlib
import re
import subprocess
import tempfile
import unittest

from test_mic_lifecycle import function

FIRMWARE = pathlib.Path(__file__).parents[1]


class Pcb06ProfileTests(unittest.TestCase):
    def compile_and_run(self, source, arguments=()):
        with tempfile.TemporaryDirectory() as temporary:
            cpp = pathlib.Path(temporary) / "test.cpp"
            exe = pathlib.Path(temporary) / "test"
            cpp.write_text(source)
            result = subprocess.run(
                [
                    "c++",
                    "-std=c++20",
                    "-I",
                    str(FIRMWARE / "relay_client"),
                    str(cpp),
                    "-o",
                    str(exe),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run(
                [str(exe), *arguments], check=False, capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_pin_profile_and_amplifier_drain(self):
        sketch = (FIRMWARE / "relay_client/relay_client.ino").read_text()
        functions = "\n".join(
            function(sketch, name) for name in ("board06SetAmp", "board06DrainSpeaker")
        )

        self.compile_and_run(
            r"""
#define RELAY_BOARD_06 1
#define RELAY_CELLULAR 1
#include "board_profile.h"
#include <cassert>
#include <cstdint>
#include <cstddef>
constexpr int INPUT = 0, HIGH = 1, LOW = 0, ESP_OK = 0;
static_assert(PIN_BTN_SPEAK == 1 && PIN_BTN_LISTEN == 5);
static_assert(PIN_BTN_VOLUME_UP == 2 && PIN_BTN_VOLUME_DOWN == 4);
static_assert(PIN_LED_R == 47 && PIN_LED_G == 48 && PIN_LED_B == 44);
static_assert(PIN_AMP_BCLK == 10 && PIN_AMP_LRC == 11 && PIN_AMP_DIN == 40 && PIN_AMP_SD == 13);
static_assert(PIN_MIC_CLK == 6 && PIN_MIC_DOUT == 7);
static_assert(PIN_USB_MUX_SEL == 39 && PIN_MODEM_PWR_EN == 21);
static_assert(RELAY_BUTTON_INPUT_MODE == INPUT);
static bool board06AmpOn = true, fail = false;
static int txChan = 1, writes = 0, disabledAfterWrites = -1;
void digitalWrite(int pin, int state) {
  assert(pin == 13);
  if (state == LOW) disabledAfterWrites = writes;
}
int i2s_channel_write(int, const void* data, size_t bytes, size_t* written, int timeout) {
  assert(timeout == 100);
  for (size_t i = 0; i < bytes; ++i) assert(static_cast<const uint8_t*>(data)[i] == 0);
  ++writes; *written = fail ? 0 : bytes;
  return fail ? 1 : ESP_OK;
}
"""
            + functions
            + r"""
int main() {
  assert(board06DrainSpeaker());
  assert(!board06AmpOn && writes == 5 && disabledAfterWrites == 5);
  board06SetAmp(true); fail = true; writes = 0;
  assert(!board06DrainSpeaker());
  assert(!board06AmpOn && writes == 1 && disabledAfterWrites == 1);
}
"""
        )

    def test_startup_registers_gpio_before_amplifier_write(self):
        sketch = (FIRMWARE / "relay_client/relay_client.ino").read_text()
        startup = sketch[sketch.index("void setup() {") :].split(
            "  Serial.begin(115200);", 1
        )[0]
        self.compile_and_run(
            r"""
#define RELAY_BOARD_06 1
#define RELAY_CELLULAR 1
#include "board_profile.h"
#include <cassert>
#include <initializer_list>
constexpr int INPUT = 1, OUTPUT = 2, HIGH = 1, LOW = 0, GPIO_MODE_OUTPUT = 2;
using gpio_num_t = int;
static bool registered[49] = {}, driven[49] = {}, preloaded[49] = {};
static int levels[49] = {};
static bool board06AmpOn = false;
void gpio_set_level(int pin, int state) {
  levels[pin] = state;
  preloaded[pin] = true;
}
void gpio_set_direction(int pin, int mode) {
  assert(mode == GPIO_MODE_OUTPUT && preloaded[pin] && levels[pin] == LOW);
  driven[pin] = true;
}
void pinMode(int pin, int mode) {
  registered[pin] = true;
  if (mode == OUTPUT) gpio_set_direction(pin, GPIO_MODE_OUTPUT);
}
// Arduino-ESP32 3.3.11 digitalWrite ignores pins not registered by pinMode.
void digitalWrite(int pin, int state) {
  if (registered[pin]) gpio_set_level(pin, state);
}
"""
            + startup
            + "}\n"
            + function(sketch, "board06SetAmp")
            + r"""
int main() {
  setup();
  assert(driven[PIN_AMP_SD] && levels[PIN_AMP_SD] == LOW);
  board06SetAmp(true);
  assert(levels[PIN_AMP_SD] == HIGH); // The silent-speaker regression.
  board06SetAmp(false);
  assert(levels[PIN_AMP_SD] == LOW);
  for (int pin : {PIN_USB_MUX_SEL, PIN_MODEM_PWR_EN, PIN_MODEM_PWRKEY,
                  PIN_MODEM_RESET, PIN_MODEM_USB_BOOT, PIN_MODEM_DTR, PIN_AMP_SD}) {
    assert(registered[pin] && driven[pin] && levels[pin] == LOW);
  }
}
"""
        )

    def test_cellular_console_never_waits_for_missing_input(self):
        sketch = (FIRMWARE / "relay_client/relay_client.ino").read_text()
        self.compile_and_run(
            r"""
#include <cassert>
#include <string>
#include <deque>
#include <vector>
#include <cstddef>
struct String {
  std::string value;
  String(const char* text): value(text) {}
  void trim() {
    size_t begin = value.find_first_not_of(" \r\n\t");
    if (begin == std::string::npos) { value.clear(); return; }
    value = value.substr(begin, value.find_last_not_of(" \r\n\t") - begin + 1);
  }
  bool operator!=(const char* other) const { return value != other; }
};
struct {
  std::deque<char> input;
  std::vector<std::string> messages;
  int available() { return input.size(); }
  int peek() { assert(!input.empty()); return input.front(); }
  int read() { assert(!input.empty()); char c=input.front(); input.pop_front(); return c; }
  void println(const char* text) { messages.emplace_back(text); }
  void feed(const std::string& text) { for (char c : text) input.push_back(c); }
} Serial;
static bool recording=false, playing=false;
std::vector<std::string> calls;
bool cellularCommand(const String& command) { calls.push_back(command.value); return true; }
"""
            + function(sketch, "board06SerialService")
            + r"""
int main() {
  assert(!board06SerialService());
  Serial.feed("C"); assert(board06SerialService());
  assert(board06SerialService() && calls.empty());
  Serial.feed("ELL STATUS\n"); assert(board06SerialService());
  assert(calls.size() == 1 && calls.back() == "CELL STATUS");
  Serial.feed("APN " + std::string(300, 'z') + "\n");
  size_t before = Serial.input.size(); board06SerialService();
  assert(before - Serial.input.size() == 32);
  while (Serial.available()) board06SerialService();
  assert(calls.size() == 1);
  recording = true;
  Serial.feed("garbage"); assert(board06SerialService() && Serial.input.empty());
  Serial.feed("CELL START\n"); board06SerialService(); assert(calls.size() == 1);
  Serial.feed("CELL STATUS\n"); board06SerialService(); assert(calls.size() == 2);
  recording = false;
  Serial.feed("APN example\r\n"); board06SerialService();
  assert(calls.size() == 3 && calls.back() == "APN example");
}
"""
        )

    def test_local_stereo_diagnostic_is_bounded_and_never_uses_network(self):
        sketch = (FIRMWARE / "relay_client/relay_client.ino").read_text()
        stats = re.search(
            r"struct Board06MicStats \{.*?\n\};", sketch, re.DOTALL
        ).group(0)
        functions = "\n".join(
            function(sketch, name)
            for name in (
                "setupMic",
                "board06MeasureMic",
                "board06PrintMic",
                "board06MicDiagnostic",
            )
        )
        harness = (FIRMWARE / "tests/pcb06_mic_diagnostic_harness.cpp").read_text()
        source = harness.replace("// SKETCH_FUNCTIONS", stats + "\n" + functions)
        for mode in ("normal", "silent-driver", "enable-failure"):
            with self.subTest(mode=mode):
                self.compile_and_run(source, [mode])


if __name__ == "__main__":
    unittest.main()
