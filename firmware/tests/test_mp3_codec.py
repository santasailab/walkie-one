import array
import pathlib
import shutil
import subprocess
import tempfile
import unittest

FIRMWARE = pathlib.Path(__file__).parents[1]
CODEC = FIRMWARE / "relay_client/src/audio"


DECODER_TONE = FIRMWARE / "tests/decoder-tone.mp3"


class Mp3CodecTests(unittest.TestCase):
    def test_tone_and_bounded_decoder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            executable = root / "codec"
            subprocess.run(
                [
                    "c++",
                    "-std=c++17",
                    "-O2",
                    "-fsanitize=address,undefined",
                    "-I",
                    str(FIRMWARE / "relay_client"),
                    str(CODEC / "mp3_codec.cpp"),
                    str(FIRMWARE / "tests/mp3_codec_harness.cpp"),
                    "-o",
                    str(executable),
                ],
                check=True,
            )
            pcm = root / "decoded.pcm"
            result = subprocess.run(
                [str(executable), str(DECODER_TONE), str(pcm)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("runtime error", result.stderr)
            # Generated 1.8-second 24 kHz mono tone plus MP3 frame padding.
            self.assertEqual(pcm.stat().st_size, 44_352 * 2)
            if not shutil.which("ffmpeg"):
                return
            reference = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-i",
                    str(DECODER_TONE),
                    "-f",
                    "s16le",
                    "pipe:1",
                ],
                check=True,
                capture_output=True,
            ).stdout
            actual = array.array("h", pcm.read_bytes())
            expected = array.array("h", reference)
            self.assertEqual(len(actual), len(expected))
            self.assertLessEqual(max(abs(a - b) for a, b in zip(actual, expected)), 2)
            # Independent encoder: an ID3v2 tag and Xing/Info frame are accepted,
            # other bitrates too; the wrong rate or channel count fails.
            for args, valid in (
                (["-ar", "24000", "-b:a", "48k", "-metadata", "title=t"], True),
                (["-ar", "24000", "-b:a", "32k", "-id3v2_version", "0"], True),
                (["-ar", "44100", "-b:a", "48k"], False),
                (["-ar", "24000", "-ac", "2", "-b:a", "48k"], False),
            ):
                sample = root / "sample.mp3"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        "sine=frequency=440:sample_rate=24000:duration=2",
                        "-ac",
                        "1",
                        "-c:a",
                        "libmp3lame",
                        *args,
                        str(sample),
                    ],
                    check=True,
                )
                checked = subprocess.run(
                    [str(executable), "--check", str(sample)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(checked.returncode == 0, valid, args)
                self.assertNotIn("runtime error", checked.stderr)
                if valid:
                    self.assertGreaterEqual(int(checked.stdout), 48000)


if __name__ == "__main__":
    unittest.main()
