"""Verify selected originals, deterministic resampling and compiled PTT PCM."""

import importlib.util
import json
import pathlib
import shutil
import struct
import subprocess
import tempfile
import unittest

FIRMWARE = pathlib.Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "generate_ptt_sounds", FIRMWARE / "tools" / "generate_ptt_sounds.py"
)
GENERATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GENERATOR)


class PttSoundTests(unittest.TestCase):
    def test_preserved_sources_and_compiled_pcm_are_exact(self):
        records = GENERATOR.sources()
        conversion = json.loads((GENERATOR.ASSETS / "conversion.json").read_text())
        converted = {}
        for asset in conversion["assets"]:
            wav = (GENERATOR.ASSETS / asset["path"]).read_bytes()
            pcm = GENERATOR.read_wav(wav, 24000)
            self.assertEqual(GENERATOR.digest(wav), asset["wav_sha256"])
            self.assertEqual(GENERATOR.digest(pcm), asset["pcm_sha256"])
            self.assertEqual(len(pcm), asset["frames"] * 2)
            converted[asset["symbol"]] = pcm
        self.assertEqual(
            GENERATOR.HEADER.read_text(), GENERATOR.render_header(records, converted)
        )
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "sounds.cpp"
            executable = pathlib.Path(directory) / "sounds"
            source.write_text("""
#include <cstdio>
#include "pcb06_ptt_sounds.h"
using namespace pcb06_ptt_sounds;
static_assert(kSampleRate == 24000);
static_assert(kPressSamples == kPress.size());
static_assert(kReleaseSamples == kRelease.size());
static_assert(kLoopSamples == kLoop.size());
static void emit(int16_t sample) {
  const auto bits = static_cast<uint16_t>(sample);
  std::putchar(bits & 255);
  std::putchar(bits >> 8);
}
int main() {
  for (auto sample : kPress) emit(sample);
  for (auto sample : kRelease) emit(sample);
  for (auto sample : kLoop) emit(sample);
}
""")
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
            )
            output = subprocess.run(
                [str(executable)], check=True, capture_output=True
            ).stdout
            self.assertEqual(output, b"".join(converted[r["symbol"]] for r in records))

    @unittest.skipUnless(
        shutil.which("ffmpeg"), "FFmpeg is needed to reproduce resampling"
    )
    def test_resampling_reproduces_committed_pcm_without_gain_changes(self):
        records, converted = GENERATOR.resample(shutil.which("ffmpeg"))
        for record in records:
            with self.subTest(sound=record["symbol"]):
                path = (
                    GENERATOR.ASSETS / "pcm24k" / pathlib.Path(record["original"]).name
                )
                expected = GENERATOR.read_wav(path.read_bytes(), 24000)
                actual = converted[record["symbol"]]
                self.assertEqual(len(actual), len(expected), "PCM frame count changed")
                sample_format = f"<{len(expected) // 2}h"
                expected_samples = struct.unpack(sample_format, expected)
                actual_samples = struct.unpack(sample_format, actual)
                # libswresample's CPU implementations can round an occasional
                # sample differently by one PCM unit. The other test still
                # requires the committed WAVs and compiled header to be exact.
                error = max(
                    abs(a - b) for a, b in zip(actual_samples, expected_samples)
                )
                self.assertLessEqual(error, 1, "Resampling differs beyond CPU rounding")


if __name__ == "__main__":
    unittest.main()
