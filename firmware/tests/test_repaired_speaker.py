import pathlib
import subprocess
import tempfile
import unittest

FIRMWARE = pathlib.Path(__file__).parents[1]


class RepairedSpeakerTests(unittest.TestCase):
    def test_streaming_matches_accepted_audition(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = pathlib.Path(temporary) / "repaired-speaker"
            subprocess.run(
                [
                    "c++",
                    "-std=c++17",
                    "-O2",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-fsanitize=address,undefined",
                    "-I",
                    str(FIRMWARE / "relay_client"),
                    str(FIRMWARE / "tests" / "repaired_speaker_harness.cpp"),
                    "-o",
                    str(executable),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [str(executable)], check=True, capture_output=True, text=True
            )
