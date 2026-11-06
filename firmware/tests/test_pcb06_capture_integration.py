"""Run production capture, release and upload paths with a stalled/failed socket."""

import pathlib
import subprocess
import tempfile
import unittest

from test_mic_lifecycle import function

FIRMWARE = pathlib.Path(__file__).parents[1]


class Pcb06CaptureIntegrationTests(unittest.TestCase):
    def test_capture_survives_slow_transport_and_aborts_incomplete_turns(self):
        sketch = (FIRMWARE / "relay_client/relay_client.ino").read_text()
        functions = "\n".join(
            function(sketch, name)
            for name in (
                "board06TalkRefused",
                "board06ObservePtt",
                "board06PlayPttChunk",
                "board06PlayCueChunk",
                "board06MicOverflow",
                "setupMic",
                "stopMicCapture",
                "board06StopCaptureLocked",
                "board06AbortCaptureLocked",
                "board06CaptureStep",
                "guardMicRelease",
                "startRecording",
                "sendTalkStart",
                "stopRecording",
                "board06TransportFailed",
                "pumpMic",
            )
        )
        harness = (FIRMWARE / "tests/pcb06_capture_integration_harness.cpp").read_text()
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "test.cpp"
            executable = pathlib.Path(directory) / "test"
            source.write_text(harness.replace("// SKETCH_FUNCTIONS", functions))
            for slot in (0, 1):
                build = subprocess.run(
                    [
                        "c++",
                        "-std=c++20",
                        "-I",
                        str(FIRMWARE / "relay_client"),
                        f"-DRELAY_PCB06_MIC_SLOT={slot}",
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
                    "ptt-during-capture",
                    "cue-during-capture",
                    "cue-already-playing",
                    "short-tap",
                    "blocked-network",
                    "gain-clipping",
                    "opening-crop",
                    "crop-only",
                    "crop-reset",
                    "muted-capture",
                    "volume-zero-capture",
                    "failed-write",
                    "failed-read",
                    "dma-overflow",
                    "failed-during-write",
                    "overflow-release",
                    "deadline",
                    "sample-cap",
                    "disconnected",
                ):
                    with self.subTest(slot=slot, mode=mode):
                        result = subprocess.run(
                            [str(executable), mode],
                            check=False,
                            capture_output=True,
                            text=True,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
