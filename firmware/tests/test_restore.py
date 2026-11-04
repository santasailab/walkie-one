import base64
import hashlib
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

MODULE_PATH = pathlib.Path(__file__).parents[1] / "tools" / "restore.py"
SPEC = importlib.util.spec_from_file_location("restore", MODULE_PATH)
restore = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(restore)


def release(image: bytes, **overrides):
    """A signed release: the signature covers board, version and SHA-256 as a message."""
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    digest = hashlib.sha256(image)
    manifest = {
        "board": "06",
        "version": "1.2.3",
        "url": "https://example.invalid/06/firmware-1.2.3.bin",
        "size": len(image),
        "sha256": digest.hexdigest(),
    }
    message = restore.signed_message("06", "1.2.3", manifest["sha256"])
    manifest["signature"] = base64.b64encode(key.sign(message)).decode()
    manifest.update(overrides)
    return manifest, public


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.image = b"\xe9" + b"firmware" * 100

    def test_valid_manifest_and_image_pass(self):
        manifest, public = release(self.image)
        loaded = restore.load_manifest(json.dumps(manifest).encode(), "06")
        restore.verify_image(loaded, self.image, public)

    def test_size_mismatch_refuses_before_flash(self):
        manifest, public = release(self.image)
        with self.assertRaisesRegex(restore.RestoreError, "size"):
            restore.verify_image(manifest, self.image + b"\0", public)
        tampered = bytearray(self.image)
        tampered[-1] ^= 1
        with self.assertRaisesRegex(restore.RestoreError, "SHA-256"):
            restore.verify_image(manifest, bytes(tampered), public)

    def test_signature_binds_the_message_not_just_the_image(self):
        manifest, public = release(self.image)
        self.assertEqual(
            restore.signed_message("06", "1.2.3", "ab" * 32),
            b"walkieone-firmware\n06\n1.2.3\n" + b"ab" * 32 + b"\n",
        )
        for other in ({"version": "1.2.4"}, {"board": "07"}):
            with self.assertRaisesRegex(restore.RestoreError, "signature"):
                restore.verify_image({**manifest, **other}, self.image, public)

    def test_paused_stage_and_missing_board_refuse(self):
        manifest, _ = release(self.image)
        paused = {"board": "06", "version": None}
        with self.assertRaisesRegex(restore.RestoreError, "no approved release"):
            restore.load_manifest(json.dumps(paused).encode(), "06")
        no_board = {k: v for k, v in manifest.items() if k != "board"}
        with self.assertRaisesRegex(restore.RestoreError, "board"):
            restore.load_manifest(json.dumps(no_board).encode(), "06")

    def test_bad_signature_and_wrong_board_refuse(self):
        manifest, _ = release(self.image)
        _, other_public = release(self.image)
        with self.assertRaisesRegex(restore.RestoreError, "signature"):
            restore.verify_image(manifest, self.image, other_public)
        with self.assertRaisesRegex(restore.RestoreError, "board"):
            restore.load_manifest(json.dumps(manifest).encode(), "07")
        for bad in ({"url": "http://example.invalid/a.bin"}, {"size": 4194305}):
            with self.assertRaises(restore.RestoreError):
                restore.load_manifest(json.dumps({**manifest, **bad}).encode(), "06")
        del manifest["signature"]
        with self.assertRaises(restore.RestoreError):
            restore.load_manifest(json.dumps(manifest).encode(), "06")

    def test_argv_never_erases_flash_or_touches_nvs(self):
        erase, write = restore.esptool_argv("/dev/x", "img.bin")
        for argv in (erase, write):
            self.assertNotIn("erase_flash", argv)
            self.assertNotIn("0x9000", argv)
        self.assertEqual(erase[-3:], ["erase_region", "0xe000", "0x2000"])
        self.assertEqual(write[-2:], ["0x10000", "img.bin"])
        self.assertEqual(erase[5:9], ["--before", "usb_reset", "--after", "no_reset"])
        self.assertEqual(write[5:9], ["--before", "no_reset", "--after", "hard_reset"])

    def test_placeholder_key_refuses(self):
        placeholder = "REPLACE_WITH_RELEASE_PUBLIC_KEY\n"
        with self.assertRaisesRegex(restore.RestoreError, "release key"):
            restore.parse_public_key(placeholder)
        _, public = release(self.image)
        encoded = base64.b64encode(public).decode()
        self.assertEqual(restore.parse_public_key(encoded + "\n"), public)


class MainTests(unittest.TestCase):
    """main() must verify before it builds or runs any esptool command."""

    def setUp(self):
        self.image = b"\xe9" + b"firmware" * 100
        self.manifest, self.public = release(self.image)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.key_path = self.dir / "release-key.pub"
        self.key_path.write_text(base64.b64encode(self.public).decode())

    def run_main(self, image: bytes, key_text: str | None = None):
        (self.dir / "firmware-1.2.3.bin").write_bytes(image)
        bundle = self.dir / "manifest.json"
        bundle.write_text(json.dumps(self.manifest))
        if key_text is not None:
            self.key_path.write_text(key_text)
        argv = ["restore.py", "--port", "/dev/x", "--bundle", str(bundle)]
        flashed = []

        def record(cmd, check):
            if cmd[-3] == "write_flash":
                flashed.append(pathlib.Path(cmd[-1]).read_bytes())

        with (
            mock.patch.object(restore, "KEY_PATH", self.key_path),
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(restore.subprocess, "run", side_effect=record) as run,
            mock.patch.object(restore.urllib.request, "urlopen") as urlopen,
        ):
            try:
                restore.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        urlopen.assert_not_called()
        return code, run, flashed

    def test_valid_bundle_runs_both_esptool_commands(self):
        code, run, flashed = self.run_main(self.image)
        self.assertEqual(code, 0)
        self.assertEqual(run.call_count, 2)
        calls = [call.args[0] for call in run.call_args_list]
        path = calls[1][-1]
        esptool = [sys.executable, "-m", "esptool", "--port", "/dev/x"]
        self.assertEqual(
            calls,
            [
                [*esptool, "--before", "usb_reset", "--after", "no_reset"]
                + ["erase_region", "0xe000", "0x2000"],
                [*esptool, "--before", "no_reset", "--after", "hard_reset"]
                + ["write_flash", "0x10000", path],
            ],
        )
        self.assertEqual(flashed, [self.image])

    def test_tampered_image_never_reaches_esptool(self):
        code, run, _ = self.run_main(self.image[:-1] + b"X")
        self.assertEqual(code, 1)
        run.assert_not_called()

    def test_placeholder_key_never_reaches_esptool(self):
        code, run, _ = self.run_main(self.image, "REPLACE_WITH_RELEASE_PUBLIC_KEY\n")
        self.assertEqual(code, 1)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class HttpsTests(unittest.TestCase):
    def test_rejects_plaintext_credentials_and_local_file_urls(self):
        for url in (
            "http://example.invalid/image.bin",
            "file:///private/file",
            "https://u:p@example.invalid/a",
            "https://example.invalid/a?secret=x",
            "https://example.invalid/a#x",
            "https://example.invalid:0/a",
        ):
            with self.subTest(url=url), self.assertRaises(restore.RestoreError):
                restore.https_url(url)

    def test_redirect_cannot_downgrade_tls(self):
        with self.assertRaises(restore.RestoreError):
            restore.HttpsRedirect().redirect_request(
                None, None, 302, "", {}, "http://example.invalid/a"
            )
