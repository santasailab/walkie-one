"""Exercise the actual button/cue paths without hardware or a network."""

import pathlib
import subprocess
import tempfile
import unittest

from test_mic_lifecycle import function

FIRMWARE = pathlib.Path(__file__).parents[1]


class Pcb06ButtonCueTests(unittest.TestCase):
    def test_button_edges_audio_isolation_and_cancellation(self):
        sketch = (FIRMWARE / "relay_client/relay_client.ino").read_text()
        functions = "\n".join(
            function(sketch, name)
            for name in (
                "board06TalkRefused",
                "board06ObservePtt",
                "board06PlayPttChunk",
                "board06PlayCueChunk",
                "buttonService",
                "board06ButtonCueService",
                "board06ServiceListenRequest",
            )
        )
        harness = (FIRMWARE / "tests/pcb06_button_cues_harness.cpp").read_text()
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "test.cpp"
            executable = pathlib.Path(directory) / "test"
            source.write_text(harness.replace("// SKETCH_FUNCTIONS", functions))
            build = subprocess.run(
                [
                    "c++",
                    "-std=c++20",
                    "-fsanitize=address,undefined",
                    "-fno-omit-frame-pointer",
                    "-I",
                    str(FIRMWARE / "relay_client"),
                    str(source),
                    "-o",
                    str(executable),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(build.returncode, 0, build.stderr)
            for mode in (
                "buttons",
                "ptt-reply-gate",
                "ptt-production",
                "ptt-mute-new-cue",
                "ptt-refused-offline",
                "ptt-refused-uploading",
                "ptt-refused-after-silent-release",
                "ptt-stalled",
                "sample-boundary",
                "listen-onset",
                "render",
                "partial",
                "mute-active",
                "zero-active",
                "mute-queued",
                "cancel-then-new",
                "bounded",
                "write-error",
                "queue",
            ):
                with self.subTest(mode=mode):
                    result = subprocess.run(
                        [str(executable), mode],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
