import pathlib
import subprocess
import tempfile
import unittest

FIRMWARE = pathlib.Path(__file__).parents[1]


class PlayedReceiptTests(unittest.TestCase):
    def test_exact_receipt_retry_and_clock_rollover(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = pathlib.Path(temporary) / "played-receipt-test"
            compile_result = subprocess.run(
                [
                    "c++",
                    "-std=c++17",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-I",
                    str(FIRMWARE / "relay_client"),
                    str(FIRMWARE / "tests" / "played_receipt_harness.cpp"),
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
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
