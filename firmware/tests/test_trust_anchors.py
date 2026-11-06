import base64
import hashlib
import pathlib
import re
import unittest

FIRMWARE = pathlib.Path(__file__).parents[1]

# SHA-256 of each public root certificate (DER), as published by ISRG.
ISRG_ROOT_X1 = "96bcec06264976f37460779acf28c5a7cfe8a3c0aae11a8ffcee05c0bddf08c6"
ISRG_ROOT_X2 = "69729b8e15a86efc177a57afb7171dfc64add28c2fca8cf1507e34453ccb1470"


class TrustAnchorTests(unittest.TestCase):
    def setUp(self):
        self.sketch = (FIRMWARE / "relay_client/relay_client.ino").read_text()

    def test_relay_trusts_exactly_both_isrg_roots(self):
        bundle = re.search(
            r'static const char \*RELAY_CA = R"CERT\((.*?)\)CERT";', self.sketch, re.S
        ).group(1)
        blocks = re.findall(
            r"-----BEGIN CERTIFICATE-----\n(.*?)-----END CERTIFICATE-----\n",
            bundle,
            re.S,
        )
        fingerprints = [
            hashlib.sha256(base64.b64decode(block)).hexdigest() for block in blocks
        ]
        # One anchor alone takes every Device offline the day the server's
        # chain stops ending at it; updates arrive over this same socket.
        self.assertEqual(sorted(fingerprints), sorted([ISRG_ROOT_X1, ISRG_ROOT_X2]))
        # Nothing but certificates: mbedTLS rejects stray text between blocks.
        self.assertEqual(
            re.sub(
                r"-----BEGIN CERTIFICATE-----\n.*?-----END CERTIFICATE-----\n",
                "",
                bundle,
                flags=re.S,
            ).strip(),
            "",
        )

    def test_verification_stays_on(self):
        self.assertIn("tls.setCACert(RELAY_CA);", self.sketch)
        self.assertNotIn("setInsecure();", self.sketch)


if __name__ == "__main__":
    unittest.main()
