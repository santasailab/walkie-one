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
