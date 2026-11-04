import pathlib
import subprocess
import tempfile
import unittest

from test_mic_lifecycle import function

FIRMWARE = pathlib.Path(__file__).parents[1]


class ReconnectPolicyTests(unittest.TestCase):
    def test_full_jitter_ceiling_and_stable_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = pathlib.Path(temporary) / "reconnect-policy-test"
            compile_result = subprocess.run(
                [
                    "c++",
                    "-std=c++17",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-I",
                    str(FIRMWARE / "relay_client"),
                    str(FIRMWARE / "tests" / "reconnect_policy_harness.cpp"),
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
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_sketch_disconnect_recovery_and_receipt_gate(self):
        source = (FIRMWARE / "relay_client/relay_client.ino").read_text()
        functions = "\n".join(
            function(source, name)
            for name in (
                "scheduleRetry",
                "retryNow",
                "connectionEnded",
                "playbackPlayStaged",
                "announceConnection",
            )
        )
        harness = (FIRMWARE / "tests/connection_lifecycle_harness.cpp").read_text()
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
