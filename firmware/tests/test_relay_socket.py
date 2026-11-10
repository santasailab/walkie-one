import pathlib
import subprocess
import tempfile
import unittest

from test_mic_lifecycle import function

FIRMWARE = pathlib.Path(__file__).parents[1]


class RelaySocketTests(unittest.TestCase):
    def test_socket_timeouts_use_their_real_units_and_writes_feed_the_watchdog(self):
        source = (FIRMWARE / "relay_client/relay_client.ino").read_text()
        functions = "\n".join(
            function(source, name)
            for name in (
                "configureRelaySocket",
                "wsSendFrame",
                "wsReadUpgradeLine",
                "wsAwaitUpgrade",
            )
        )
        harness = (FIRMWARE / "tests/relay_socket_harness.cpp").read_text()
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

    def test_dial_applies_the_socket_configuration(self):
        source = (FIRMWARE / "relay_client/relay_client.ino").read_text()
        dial = function(source, "wsConnect")
        self.assertIn("configureRelaySocket();", dial)
        self.assertLess(
            dial.index("configureRelaySocket();"), dial.index("tls.connect(HOST, PORT)")
        )
        self.assertIn("if (!wsAwaitUpgrade()) {", dial)
        # The only timeout calls live in configureRelaySocket.
        self.assertNotIn("tls.setTimeout(", dial)
        self.assertEqual(source.count("tls.setTimeout("), 1)


if __name__ == "__main__":
    unittest.main()
