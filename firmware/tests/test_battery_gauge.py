import pathlib
import subprocess
import tempfile
import unittest

FIRMWARE = pathlib.Path(__file__).parents[1]


class BatteryGaugeTests(unittest.TestCase):
    def test_real_i2c_read_conversion_and_unknown_readings(self):
        self.run_harness("battery_gauge_harness.cpp")

    def test_power_state_debounce_and_unplug_history(self):
        self.run_harness("power_status_harness.cpp")

    def test_low_battery_warnings_volume_cap_and_empty(self):
        self.run_harness("battery_watch_harness.cpp")

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
