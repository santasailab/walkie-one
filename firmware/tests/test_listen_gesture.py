import pathlib
import subprocess
import tempfile
import unittest

from test_mic_lifecycle import function

FIRMWARE = pathlib.Path(__file__).parents[1]


class ListenGestureTests(unittest.TestCase):
    def test_sketch_requires_a_fresh_click_for_each_ready_reply(self):
        source = (FIRMWARE / "relay_client/relay_client.ino").read_text()
        functions = "\n".join(
            function(source, name)
            for name in (
                "jsonFind",
                "jsonIs",
                "jsonStr",
                "jsonInt",
                "jsonTrue",
                "jsonFalse",
                "playbackClear",
                "playbackStart",
                "playbackCommit",
                "playbackPlayStaged",
                "pausePlayback",
                "abandonPlayback",
                "handleMessage",
                "museBlocksTalk",
                "buttonService",
            )
        )
        harness = (FIRMWARE / "tests/listen_lifecycle_harness.cpp").read_text()
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
            for scenario in (
                "no-click",
                "idle-single-click",
                "idle-double-click",
                "during-download",
                "held-download",
                "replacement-during-hold",
                "receipt-confirmation",
            ):
                with self.subTest(scenario=scenario):
                    result = subprocess.run(
                        [str(executable), scenario],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_release_can_only_play_a_reply_staged_at_press(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = pathlib.Path(temporary) / "listen-gesture-test"
            compile_result = subprocess.run(
                [
                    "c++",
                    "-std=c++17",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-I",
                    str(FIRMWARE / "relay_client"),
                    str(FIRMWARE / "tests" / "listen_gesture_harness.cpp"),
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
