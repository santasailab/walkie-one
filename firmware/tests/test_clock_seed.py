import pathlib
import subprocess
import tempfile
import unittest

from test_mic_lifecycle import function

FIRMWARE = pathlib.Path(__file__).parents[1]


class ClockSeedTests(unittest.TestCase):
    def test_network_time_beats_the_build_stamp_and_never_moves_backward(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = pathlib.Path(temporary) / "clock-seed-test"
            built = subprocess.run(
                [
                    "c++",
                    "-std=c++17",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-I",
                    str(FIRMWARE / "relay_client"),
                    str(FIRMWARE / "tests" / "clock_seed_harness.cpp"),
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

    def test_every_dial_reconsiders_the_clock(self):
        sketch = (FIRMWARE / "relay_client/relay_client.ino").read_text()
        ready = function(sketch, "clockReady")
        # Outside the once-per-boot block: a later modem session may bring
        # the network time that the first one lacked.
        once = ready[ready.index("if (!started) {") : ready.index("started = true;")]
        self.assertNotIn("seedClock();", once)
        self.assertIn("seedClock();", ready)
        seed = function(sketch, "seedClock")
        self.assertIn("cellularNetworkTime(", seed)
        self.assertIn("clockSeed(", seed)
        # SNTP reports its first answer; from then on it owns the clock.
        self.assertIn("sntpSynced)", seed)
        self.assertLess(
            once.index("sntp_set_time_sync_notification_cb("), once.index("configTime(")
        )


if __name__ == "__main__":
    unittest.main()
