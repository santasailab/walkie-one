import pathlib
import re
import subprocess
import tempfile
import unittest

FIRMWARE = pathlib.Path(__file__).parents[1]


def function(source, name):
    definition = re.search(
        rf"^static [^\n]*\b{re.escape(name)}\([^;{{}}]*\)\s*{{", source, re.MULTILINE
    )
    assert definition is not None, f"Missing sketch function: {name}"
    depth, end = 1, definition.end()
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[definition.start() : end]


class BatteryGaugeTests(unittest.TestCase):
    def test_real_i2c_read_conversion_and_unknown_readings(self):
        self.run_harness("battery_gauge_harness.cpp")

    def test_power_state_debounce_and_unplug_history(self):
        self.run_harness("power_status_harness.cpp")

    def test_low_battery_warnings_volume_cap_and_empty(self):
        self.run_harness("battery_watch_harness.cpp")

    def test_empty_battery_boot_stays_off_before_the_modem_is_powered(self):
        sketch = (FIRMWARE / "relay_client/relay_client.ino").read_text()
        harness = (FIRMWARE / "tests/battery_start_harness.cpp").read_text()
        with tempfile.TemporaryDirectory() as temporary:
            source = pathlib.Path(temporary) / "battery_start.cpp"
            source.write_text(
                harness.replace(
                    "// SKETCH_FUNCTIONS", function(sketch, "board06StartsOff")
                )
            )
            self.run_harness(source)
        # The check runs before the modem, speaker or microphone are started.
        setup = sketch[sketch.index("void setup() {") : sketch.index("void loop() {")]
        self.assertLess(
            setup.index("asleep = board06StartsOff(asleep);"),
            setup.index("cellularSetup()"),
        )
        self.assertLess(
            setup.index("asleep = board06StartsOff(asleep);"),
            setup.index("if (asleep) {"),
        )

    def run_harness(self, harness):
        with tempfile.TemporaryDirectory() as temporary:
            executable = pathlib.Path(temporary) / "battery-gauge-test"
            compile_result = subprocess.run(
                [
                    "c++",
                    "-std=c++17",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-I",
                    str(FIRMWARE / "relay_client"),
                    str(FIRMWARE / "tests" / harness),
                    "-o",
                    str(executable),
                ],
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            run = subprocess.run(
                [str(executable)], capture_output=True, check=False, text=True
            )
            self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == "__main__":
    unittest.main()
