"""Exercise bounded Talk buffering independently of hardware/network timing."""

import pathlib
import subprocess
import tempfile
import unittest

FIRMWARE = pathlib.Path(__file__).parents[1]


class Pcb06CaptureSpoolTests(unittest.TestCase):
    def test_bounded_capture_preserves_samples_and_delivery_boundaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = pathlib.Path(temporary) / "capture_spool"
            subprocess.run(
                [
                    "c++",
                    "-std=c++17",
                    "-O1",
                    "-g",
                    "-fsanitize=address,undefined",
                    "-I",
                    str(FIRMWARE / "relay_client"),
                    str(FIRMWARE / "tests/pcb06_capture_spool_harness.cpp"),
                    "-o",
                    str(executable),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            result = subprocess.run(
                [str(executable)], capture_output=True, text=True, check=False
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("runtime error", result.stderr)


if __name__ == "__main__":
    unittest.main()
