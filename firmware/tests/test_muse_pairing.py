import os
import pathlib
import subprocess
import tempfile
import unittest

FIRMWARE = pathlib.Path(__file__).parents[1]


def prefix(name):
    """A local mbedTLS 3.x or cJSON install; the Device ships its own."""
    for root in (
        os.environ.get(name.upper().replace("@", "").replace("3", "") + "_PREFIX"),
        f"/opt/homebrew/opt/{name}",
        f"/usr/local/opt/{name}",
    ):
        if root and pathlib.Path(root, "include").is_dir():
            return pathlib.Path(root)
    return None


MBEDTLS = prefix("mbedtls@3")
CJSON = prefix("cjson")


@unittest.skipUnless(
    MBEDTLS and CJSON, "needs mbedTLS 3 and cJSON: brew install mbedtls@3 cjson"
)
class MusePairingTests(unittest.TestCase):
    def test_pairing_matches_metas_vector_and_refuses_forgeries(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = pathlib.Path(temporary) / "muse-pairing-test"
            compile_result = subprocess.run(
                [
                    "c++",
                    "-std=c++17",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-I",
                    str(FIRMWARE / "relay_client"),
                    "-I",
                    str(MBEDTLS / "include"),
                    "-I",
                    str(CJSON / "include" / "cjson"),
                    str(FIRMWARE / "tests" / "muse_pairing_harness.cpp"),
                    str(FIRMWARE / "relay_client" / "muse_pairing.cpp"),
                    "-L",
                    str(MBEDTLS / "lib"),
                    "-L",
                    str(CJSON / "lib"),
                    "-lmbedcrypto",
                    "-lcjson",
                    "-o",
                    str(executable),
                ],
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)

            result = subprocess.run(
                [str(executable)], capture_output=True, check=False, text=True
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
