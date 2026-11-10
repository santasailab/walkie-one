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


class CellularSetupTests(unittest.TestCase):
    def run_tool(self, lines, **arguments):
        import contextlib
        import io
        import itertools
        from unittest import mock

        board = mock.MagicMock()
        board.__enter__.return_value = board
        board.readline.side_effect = itertools.chain(
            (line.encode() for line in lines), itertools.repeat(b"")
        )
        output = io.StringIO()
        clock = iter(range(10_000))
        with (
            mock.patch.object(provision.serial, "Serial", return_value=board),
            mock.patch.object(provision.time, "sleep"),
            mock.patch.object(provision.time, "monotonic", lambda: next(clock) / 10),
            contextlib.redirect_stdout(output),
        ):
            try:
                provision.provision("/dev/test", timeout=12, **arguments)
                error = None
            except (RuntimeError, TimeoutError) as raised:
                error = raised
        sent = [call.args[0].decode() for call in board.write.call_args_list]
        return sent, output.getvalue(), error

    def test_apn_follows_the_same_rule_as_the_firmware(self):
        for good in ("internet", "a.b-c.1", "A" * 100):
            self.assertEqual(provision.validate_apn(good), good)
        for bad in ("", "a b", "a\rAT+CFUN=0", 'a";AT', "a_1", "A" * 101, "a\n"):
            with self.assertRaises(ValueError):
                provision.validate_apn(bad)

    def test_credential_then_apn_then_start_in_one_session(self):
        credential = "v1.pair-42.device." + "A" * 43
        sent, output, error = self.run_tool(
            [
                "[provision] OK pair=pair-42",
                "[cell] off",
                "[cell] radio supply off; stop/save queued",
                "[cell] settings saved; auto-start=off",
                "[cell] APN save queued; radio off. CELL START enables and saves",
                "[cell] start/save queued; enabled setting survives restart",
                "[cell] settings saved; auto-start=on",
            ],
            credential=credential,
            pair_id="pair-42",
            apn="private.apn",
        )
        self.assertIsNone(error)
        self.assertEqual(
            sent,
            [
                f"PROVISION {credential}\n",
                "CELL STOP\n",
                "CELL APN private.apn\n",
                "CELL START\n",
            ],
        )
        self.assertIn("Cellular settings stored", output)
        # The APN identifies a SIM plan: like the firmware, never echo it.
        self.assertNotIn("private.apn", output)

    def test_apn_alone_leaves_the_stored_credential(self):
        sent, output, error = self.run_tool(
            [
                "[cell] radio supply off; stop/save queued",
                "[cell] APN save queued; radio off. CELL START enables and saves",
                "[cell] start/save queued; enabled setting survives restart",
                "[cell] settings saved; auto-start=on",
            ],
            credential=None,
            pair_id=None,
            apn="internet",
        )
        self.assertIsNone(error)
        self.assertEqual(sent, ["CELL STOP\n", "CELL APN internet\n", "CELL START\n"])
        self.assertNotIn("credential provisioned", output)

    def test_device_refusal_stops_before_the_radio_is_started(self):
        for refusal in (
            "[cell] invalid APN; use 1-100 letters, digits, dots or hyphens",
            "[cell] finish Talk/playback before changing modem settings",
            "[cell] unknown command; use CELL STATUS/START/STOP or CELL APN <value>",
        ):
            sent, output, error = self.run_tool(
                ["[cell] radio supply off; stop/save queued", refusal],
                credential=None,
                pair_id=None,
                apn="internet",
            )
            self.assertIsInstance(error, RuntimeError)
            self.assertNotIn("CELL START\n", sent)
            self.assertNotIn("internet", str(error) + output)

    def test_success_waits_for_the_settings_to_reach_storage(self):
        accepted = [
            "[cell] radio supply off; stop/save queued",
            "[cell] APN save queued; radio off. CELL START enables and saves",
            "[cell] start/save queued; enabled setting survives restart",
        ]
        arguments = {"credential": None, "pair_id": None, "apn": "internet"}
        # Queued is not saved: the firmware writes the settings afterwards.
        _, output, error = self.run_tool(accepted, **arguments)
        self.assertIsInstance(error, TimeoutError)
        self.assertNotIn("stored", output)
        # An earlier command's save, with the radio still off, is not this one.
        _, output, error = self.run_tool(
            accepted + ["[cell] settings saved; auto-start=off"], **arguments
        )
        self.assertIsInstance(error, TimeoutError)
        _, output, error = self.run_tool(
            accepted + ["[cell] configuration could not be saved; radio off"],
            **arguments,
        )
        self.assertIsInstance(error, RuntimeError)
        self.assertNotIn("stored", output)

    def test_silent_device_times_out(self):
        sent, _, error = self.run_tool(
            [], credential=None, pair_id=None, apn="internet"
        )
        self.assertIsInstance(error, TimeoutError)
        self.assertEqual(sent, ["CELL STOP\n"])


if __name__ == "__main__":
    unittest.main()
