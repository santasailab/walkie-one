import pathlib
import subprocess
import tempfile
import unittest

FIRMWARE = pathlib.Path(__file__).parents[1]


class PttGestureTests(unittest.TestCase):
    def test_short_taps_stay_local_and_three_open_pairing(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = pathlib.Path(temporary) / "ptt-gesture-test"
            compile_result = subprocess.run(
                [
                    "c++",
                    "-std=c++17",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-I",
                    str(FIRMWARE / "relay_client"),
                    str(FIRMWARE / "tests" / "ptt_gesture_harness.cpp"),
                    "-o",
                    str(executable),
                ],
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)

            result = subprocess.run(
                [str(executable)], capture_output=True, check=False, text=True
            )
            self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
