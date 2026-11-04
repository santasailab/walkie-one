"""Check the local PTT envelope, including loops and interruption boundaries."""

import pathlib
import subprocess
import tempfile
import unittest

FIRMWARE = pathlib.Path(__file__).parents[1]


class Pcb06PttPlayerTests(unittest.TestCase):
    def test_envelope_and_interruptions(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = pathlib.Path(directory) / "ptt-player"
            built = subprocess.run(
                [
                    "c++",
                    "-std=c++20",
                    "-fsanitize=address,undefined",
                    "-I",
                    str(FIRMWARE / "relay_client"),
                    str(FIRMWARE / "tests/pcb06_ptt_player_harness.cpp"),
                    "-o",
                    str(executable),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            for mode in (
                "loop",
                "varied-loop",
                "signed-close",
                "mute",
                "quick-tap",
                "repress-open",
                "repress-bed",
                "repress-close",
                "repress-drain",
                "release-during-flush",
                "partial",
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
