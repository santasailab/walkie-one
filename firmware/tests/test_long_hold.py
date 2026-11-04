import pathlib
import subprocess
import tempfile
import unittest

from test_mic_lifecycle import function

FIRMWARE = pathlib.Path(__file__).parents[1]


class LongHoldTests(unittest.TestCase):
    def test_hold_fires_once_at_threshold_and_rearms_on_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = pathlib.Path(temporary) / "long-hold-test"
            compile_result = subprocess.run(
                [
                    "c++",
                    "-std=c++17",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-I",
                    str(FIRMWARE / "relay_client"),
                    str(FIRMWARE / "tests" / "long_hold_harness.cpp"),
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

    def test_power_task_restarts_into_off_and_on_across_held_buttons(self):
        source = (FIRMWARE / "relay_client/relay_client.ino").read_text()
        functions = "\n".join(
            function(source, name) for name in ("restartInto", "powerTask")
        )
        harness = (FIRMWARE / "tests/power_lifecycle_harness.cpp").read_text()
        with tempfile.TemporaryDirectory() as temporary:
            cpp = pathlib.Path(temporary) / "test.cpp"
            executable = pathlib.Path(temporary) / "test"
            cpp.write_text(harness.replace("// SKETCH_FUNCTIONS", functions))
            built = subprocess.run(
                [
                    "c++",
                    "-std=c++17",
                    "-I",
                    str(FIRMWARE / "relay_client"),
                    str(cpp),
                    "-o",
                    str(executable),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            tested = subprocess.run(
                [str(executable)], capture_output=True, text=True, check=False
            )
            self.assertEqual(tested.returncode, 0, tested.stderr)


if __name__ == "__main__":
    unittest.main()
