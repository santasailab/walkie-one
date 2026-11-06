import pathlib
import subprocess
import tempfile
import unittest

from test_mic_lifecycle import function

FIRMWARE = pathlib.Path(__file__).parents[1]


class DataPathWatchTests(unittest.TestCase):
    def test_modem_restart_only_after_sustained_unreachable_relay(self):
        source = (FIRMWARE / "relay_client/relay_client.ino").read_text()
        harness = (FIRMWARE / "tests/data_path_watch_harness.cpp").read_text()
        with tempfile.TemporaryDirectory() as temporary:
            cpp = pathlib.Path(temporary) / "test.cpp"
            executable = pathlib.Path(temporary) / "test"
            cpp.write_text(
                harness.replace(
                    "// SKETCH_FUNCTIONS", function(source, "dataPathUnreachable")
                )
            )
            built = subprocess.run(
                [
                    "c++",
                    "-std=c++17",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-fsanitize=address,undefined",
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

    def test_loop_reports_failed_dials_silence_and_recovery(self):
        source = (FIRMWARE / "relay_client/relay_client.ino").read_text()
        loop = source[source.index("void loop() {") :]
        # A dial that never reached the relay, and an open socket gone silent.
        self.assertEqual(loop.count("dataPathUnreachable();"), 2)
        # A relay that answered, even to refuse, proves the bearer works.
        self.assertIn("if (wsReachedRelay) dataPath.reached();", loop)
        self.assertIn("dataPath.bearerDown();", loop)
        dial = function(source, "wsConnect")
        self.assertIn(
            "wsReachedRelay = dialReachedRelay(false, tls.lastError(err, sizeof(err)));",
            dial,
        )
        self.assertIn(
            "static_assert(kTlsCertificateRejected == MBEDTLS_ERR_X509_CERT_VERIFY_FAILED",
            source,
        )


if __name__ == "__main__":
    unittest.main()
