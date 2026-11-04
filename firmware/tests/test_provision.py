import importlib.util
import pathlib
import unittest

MODULE_PATH = pathlib.Path(__file__).parents[1] / "tools" / "provision.py"
SPEC = importlib.util.spec_from_file_location("provision", MODULE_PATH)
provision = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provision)


class ExtractCredentialTests(unittest.TestCase):
    def setUp(self):
        self.signature = "A" * 43
        self.credential = f"v1.pair-42.device.{self.signature}"

    def test_bare_credential(self):
        self.assertEqual(
            provision.extract_credential(self.credential),
            (self.credential, "pair-42"),
        )

    def test_mint_output(self):
        raw = f"pair pair-42\nmac ignored\ndevice {self.credential}\n"
        self.assertEqual(
            provision.extract_credential(raw),
            (self.credential, "pair-42"),
        )

    def test_rejects_mac_credential(self):
        with self.assertRaises(ValueError):
            provision.extract_credential(self.credential.replace(".device.", ".mac."))

    def test_rejects_multiple_credentials(self):
        with self.assertRaises(ValueError):
            provision.extract_credential(f"{self.credential}\n{self.credential}")


if __name__ == "__main__":
    unittest.main()


class ProvisionOutputTests(unittest.TestCase):
    def test_device_output_and_identifier_are_never_echoed(self):
        import contextlib
        import io
        import secrets
        from unittest import mock

        pair = "private-device"
        credential = f"v1.{pair}.device.{secrets.token_urlsafe(32)}"
        for success in (True, False):
            board = mock.MagicMock()
            board.__enter__.return_value = board
            board.readline.side_effect = [
                credential.encode(),
                (
                    f"[provision] OK pair={pair}"
                    if success
                    else f"[provision] ERROR {credential}"
                ).encode(),
            ]
            output = io.StringIO()
            with (
                mock.patch.object(provision.serial, "Serial", return_value=board),
                mock.patch.object(provision.time, "sleep"),
                contextlib.redirect_stdout(output),
            ):
                if success:
                    provision.provision("/dev/test", credential, pair, 12)
                    self.assertIn("successfully", output.getvalue())
                else:
                    with self.assertRaisesRegex(
                        RuntimeError, "device rejected provisioning"
                    ) as error:
                        provision.provision("/dev/test", credential, pair, 12)
                    self.assertNotIn(credential, str(error.exception))
            self.assertNotIn(credential, output.getvalue())
            self.assertNotIn(pair, output.getvalue())
            self.assertNotIn("/dev/test", output.getvalue())
