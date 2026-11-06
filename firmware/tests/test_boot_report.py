import pathlib
import subprocess
import tempfile
import unittest

from test_mic_lifecycle import function

FIRMWARE = pathlib.Path(__file__).parents[1]


class BootReportTests(unittest.TestCase):
    def test_reset_reasons_map_to_causes_and_self_restarts_are_quiet(self):
        sketch = (FIRMWARE / "relay_client/relay_client.ino").read_text()
        harness = (FIRMWARE / "tests/boot_report_harness.cpp").read_text()
        with tempfile.TemporaryDirectory() as temporary:
            cpp = pathlib.Path(temporary) / "test.cpp"
            executable = pathlib.Path(temporary) / "test"
            cpp.write_text(
                harness.replace("// SKETCH_FUNCTIONS", function(sketch, "bootCause"))
            )
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

    def test_setup_reports_the_cause_and_never_chirps_after_a_self_restart(self):
        sketch = (FIRMWARE / "relay_client/relay_client.ino").read_text()
        setup = sketch[sketch.index("void setup() {") : sketch.index("void loop() {")]
        self.assertIn('Serial.printf("[boot] cause=%s', setup)
        # Every boot chirp is behind the quiet-boot decision.
        self.assertEqual(setup.count("bootChirp();"), 1)
        self.assertIn("if (!quietBoot) bootChirp();", setup)
        self.assertIn("bootIsUnattended(cause)", setup)
        # A run of brownout restarts ends switched off, decided before anything
        # is powered, and a boot that runs well forgets the run.
        self.assertLess(
            setup.index("brownoutStaysOff(brownoutRecord, cause)"),
            setup.index("cellularSetup()"),
        )
        self.assertIn("softOffMarker = kSoftOffMarker;", setup)
        loop = sketch[sketch.index("void loop() {") :]
        self.assertIn("brownoutSettled(brownoutRecord);", loop)
        self.assertIn("static RTC_NOINIT_ATTR BrownoutRecord brownoutRecord;", sketch)
        # Field diagnostics: the low-water mark, not only the current heap.
        self.assertIn("minheap=%lu", sketch)


if __name__ == "__main__":
    unittest.main()
