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


if __name__ == "__main__":
    unittest.main()
