import pathlib
import subprocess
import tempfile
import unittest

FIRMWARE = pathlib.Path(__file__).parents[1]


class WebSocketReaderTests(unittest.TestCase):
    def test_fragmented_frames_partial_reads_controls_and_deadlines(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = pathlib.Path(temporary) / "websocket-reader-test"
            compile_result = subprocess.run(
                [
                    "c++",
                    "-std=c++17",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-I",
                    str(FIRMWARE / "relay_client"),
                    str(FIRMWARE / "tests" / "websocket_reader_harness.cpp"),
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
