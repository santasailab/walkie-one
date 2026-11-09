import pathlib
import subprocess
import tempfile
import unittest

FIRMWARE = pathlib.Path(__file__).parents[1]


class CellularDriverTests(unittest.TestCase):
    def test_driver_with_simulated_uart_ppp_clock_and_storage(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            for header in [
                "Arduino.h",
                "HardwareSerial.h",
                "Network.h",
                "PPP.h",
                "Preferences.h",
            ]:
                (directory / header).write_text('#include "cellular_test_arduino.h"\n')
            executable = directory / "cellular-driver-test"
            compile_result = subprocess.run(
                [
                    "c++",
                    "-std=c++17",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-fsanitize=address,undefined",
                    "-I",
                    str(directory),
                    "-I",
                    str(FIRMWARE / "cellular" / "main"),
                    "-I",
                    str(FIRMWARE / "tests"),
                    str(FIRMWARE / "tests" / "cellular_driver_harness.cpp"),
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

    def test_every_restart_powers_the_modem_down_first(self):
        # setup() latches the modem supply low within milliseconds of a
        # restart, so a live module would be cut mid-operation unless the
        # worker has powered it down before esp_restart().
        from test_mic_lifecycle import function

        sketch = (FIRMWARE / "relay_client/relay_client.ino").read_text()
        wait = function(sketch, "modemOffForRestart")
        self.assertIn("cellularShutdown();", wait)
        self.assertIn("cellularPoweredDown()", wait)
        self.assertIn("kPowerOffBudgetMs", wait)
        into = function(sketch, "restartInto")
        self.assertLess(
            into.index("modemOffForRestart();"), into.index("esp_restart();")
        )
        buttons = sketch[sketch.index("[buttons] saved; restarting") :]
        self.assertLess(
            buttons.index("modemOffForRestart();"), buttons.index("ESP.restart();")
        )


if __name__ == "__main__":
    unittest.main()
