import pathlib
import unittest

FIRMWARE = pathlib.Path(__file__).parents[1]


class ConsoleSecretsTests(unittest.TestCase):
    def test_credential_variable_never_reaches_serial(self):
        source = (FIRMWARE / "relay_client" / "relay_client.ino").read_text()
        for line in source.splitlines():
            if "Serial.print" in line:
                self.assertNotIn("relayCredential", line)
                self.assertNotIn("credential.c_str()", line)


if __name__ == "__main__":
    unittest.main()
