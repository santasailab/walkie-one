"""Run the sketch's real capture lifecycle against an instrumented I2S driver."""

import pathlib
import re
import subprocess
import tempfile
import unittest

FIRMWARE = pathlib.Path(__file__).parents[1]


def function(source, name):
    definition = re.search(
        rf"^static [^\n]*\b{re.escape(name)}\([^;{{}}]*\)\s*{{", source, re.MULTILINE
    )
    assert definition is not None, f"Missing sketch function: {name}"
    start = definition.start()
    body = definition.end() - 1
    depth = 1
    end = body + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


class MicLifecycleTests(unittest.TestCase):
    def test_capture_is_idle_at_boot_and_stops_before_network_flush(self):
        self.check_capture_lifecycle([])

    def check_capture_lifecycle(self, flags):
        source = (FIRMWARE / "relay_client/relay_client.ino").read_text()
        functions = "\n".join(
            function(source, name)
            for name in (
                "setupMic",
                "stopMicCapture",
                "guardMicRelease",
                "startRecording",
                "sendTalkStart",
                "stopRecording",
                "pumpMic",
            )
        )
        limits = re.search(
            r"#ifdef RELAY_BOARD_06\nstatic const uint32_t MAX_RECORDING_MS.*?"
            r"static const uint32_t MAX_RECORDING_SAMPLES[^\n]*",
            source,
            re.DOTALL,
        ).group(0)
        functions = limits + "\n" + functions
        harness = (FIRMWARE / "tests/mic_lifecycle_harness.cpp").read_text()
        with tempfile.TemporaryDirectory() as temporary:
            cpp = pathlib.Path(temporary) / "test.cpp"
            executable = pathlib.Path(temporary) / "test"
            cpp.write_text(harness.replace("// SKETCH_FUNCTIONS", functions))
            built = subprocess.run(
                [
                    "c++",
                    "-std=c++20",
                    "-I",
                    str(FIRMWARE / "relay_client"),
                    *flags,
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
                [str(executable)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(tested.returncode, 0, tested.stderr)


if __name__ == "__main__":
    unittest.main()
