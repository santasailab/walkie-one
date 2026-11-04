import base64
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

TOOLS = pathlib.Path(__file__).parents[1] / "tools"
SPEC = importlib.util.spec_from_file_location("release", TOOLS / "release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
restore = sys.modules["restore"]


def key_pair():
    key = Ed25519PrivateKey.generate()
    seed = base64.b64encode(key.private_bytes_raw()).decode()
    return seed, key.public_key().public_bytes_raw()


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.image = b"\xe9" + b"firmware" * 100
        self.url = "https://example.blob.core.windows.net/updates/firmware/06/releases/1.2.3/WalkieOne-06-1.2.3.bin"

    def test_signed_manifest_passes_the_restore_check(self):
        seed, public = key_pair()
        manifest = release.sign(self.image, "1.2.3", self.url, seed, public)
        loaded = restore.load_manifest(json.dumps(manifest).encode(), "06")
        restore.verify_image(loaded, self.image, public)
        self.assertEqual(manifest["size"], len(self.image))
        self.assertRegex(manifest["signature"], r"^[A-Za-z0-9+/]{86}==$")

    def test_refuses_a_key_that_does_not_match_the_published_one(self):
        seed, _ = key_pair()
        _, other = key_pair()
        with self.assertRaisesRegex(release.ReleaseError, "does not match"):
            release.sign(self.image, "1.2.3", self.url, seed, other)
        with self.assertRaisesRegex(release.ReleaseError, "base64|32-byte"):
            release.sign(self.image, "1.2.3", self.url, "", other)

    def test_refuses_bad_versions_sizes_and_urls(self):
        seed, public = key_pair()
        for version in ("1.2", "1.2.3-pcb06", "10000.0.0"):
            with self.assertRaisesRegex(release.ReleaseError, "version"):
                release.sign(self.image, version, self.url, seed, public)
        with self.assertRaisesRegex(release.ReleaseError, "4 MiB"):
            release.sign(b"\0" * (4 * 1024 * 1024 + 1), "1.2.3", self.url, seed, public)
        for url in ("http://example.com/a.bin", self.url + "?sv=1"):
            with self.assertRaisesRegex(release.ReleaseError, "https"):
                release.sign(self.image, "1.2.3", url, seed, public)

    def test_cli_signs_with_the_environment_key_and_writes_a_pause(self):
        seed, public = key_pair()
        with tempfile.TemporaryDirectory() as temporary:
            folder = pathlib.Path(temporary)
            (folder / "image.bin").write_bytes(self.image)
            (folder / "key.pub").write_text(base64.b64encode(public).decode())
            run = subprocess.run(
                [
                    sys.executable,
                    str(TOOLS / "release.py"),
                    "sign",
                    "--image",
                    str(folder / "image.bin"),
                    "--version",
                    "1.2.3",
                    "--url",
                    self.url,
                    "--out",
                    str(folder / "test.json"),
                    "--public-key",
                    str(folder / "key.pub"),
                ],
                env={**os.environ, "FIRMWARE_SIGNING_KEY": seed},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertNotIn(seed, run.stdout + run.stderr)
            manifest = json.loads((folder / "test.json").read_text())
            restore.verify_image(manifest, self.image, public)
            run = subprocess.run(
                [
                    sys.executable,
                    str(TOOLS / "release.py"),
                    "pause",
                    "--out",
                    str(folder / "p.json"),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(
                json.loads((folder / "p.json").read_text()),
                {"board": "06", "version": None},
            )


if __name__ == "__main__":
    unittest.main()
