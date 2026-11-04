"""Prove that firmware click arrays preserve the supplied PCM exactly."""

import importlib.util
import pathlib
import subprocess
import tempfile
import unittest

FIRMWARE = pathlib.Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "generate_button_sounds", FIRMWARE / "tools" / "generate_button_sounds.py"
)
GENERATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GENERATOR)


class ButtonSoundTests(unittest.TestCase):
    def test_generated_header_is_current(self):
        self.assertEqual(GENERATOR.HEADER.read_text(), GENERATOR.render_header())

    def test_compiled_arrays_match_original_pcm(self):
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "sounds.cpp"
            executable = pathlib.Path(directory) / "sounds"
            source.write_text(
                """
#include <cstdio>
#include "pcb06_button_sounds.h"
using namespace pcb06_sounds;
static_assert(kSampleRate == 24000);
static_assert(kPlayTalkSamples == 912);
static_assert(kVolumeTickSamples == 552);
static void emit(int16_t sample) {
  const auto bits = static_cast<uint16_t>(sample);
  std::putchar(bits & 255);
  std::putchar(bits >> 8);
}
int main() {
  for (auto sample : kPlayTalk) emit(sample);
  for (auto sample : kVolumeTick) emit(sample);
}
"""
            )
            subprocess.run(
                [
                    "c++",
                    "-std=c++17",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-I",
                    str(FIRMWARE / "relay_client"),
                    str(source),
                    "-o",
                    str(executable),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            actual = subprocess.run(
                [str(executable)], check=True, capture_output=True
            ).stdout
            expected = b"".join(
                GENERATOR.read_pcm(GENERATOR.ASSETS / filename, frames, expected_hash)
                for filename, _, frames, expected_hash in GENERATOR.SOUNDS
            )
            self.assertEqual(len(actual), 2928)
            self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
