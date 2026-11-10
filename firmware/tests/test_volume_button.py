import pathlib
import subprocess
import tempfile
import unittest

from test_battery_gauge import function

FIRMWARE = pathlib.Path(__file__).parents[1]


class VolumeButtonTests(unittest.TestCase):
    def test_debounce_hold_repeat_and_rollover(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = pathlib.Path(temporary) / "volume-button-test"
            compile_result = subprocess.run(
                [
                    "c++",
                    "-std=c++17",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-I",
                    str(FIRMWARE / "relay_client"),
                    str(FIRMWARE / "tests" / "volume_button_harness.cpp"),
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

    def test_volume_survives_restarts_with_one_write_per_change(self):
        sketch = (FIRMWARE / "relay_client/relay_client.ino").read_text()
        functions = "\n".join(
            function(sketch, name) for name in ("loadVolume", "volumeSaveService")
        )
        harness = (FIRMWARE / "tests/volume_store_harness.cpp").read_text()
        with tempfile.TemporaryDirectory() as temporary:
            cpp = pathlib.Path(temporary) / "test.cpp"
            executable = pathlib.Path(temporary) / "test"
            cpp.write_text(harness.replace("// SKETCH_FUNCTIONS", functions))
            built = subprocess.run(
                [
                    "c++",
                    "-std=c++17",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-I",
                    str(FIRMWARE / "relay_client"),
                    str(cpp),
                    "-o",
                    str(executable),
                ],
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            tested = subprocess.run(
                [str(executable)], capture_output=True, check=False, text=True
            )
            self.assertEqual(tested.returncode, 0, tested.stderr)
        # Restored before the boot chirp and serviced every loop.
        setup, loop = sketch[sketch.index("void setup() {") :].split("void loop() {")
        self.assertIn("loadVolume();", setup)
        self.assertIn("volumeSaveService();", loop)


if __name__ == "__main__":
    unittest.main()
