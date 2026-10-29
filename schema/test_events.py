#!/usr/bin/env python3
"""Validate the device contract: every example passes, bad payloads fail.

    pip3 install jsonschema && python3 schema/test_events.py

Not a runtime dependency - Relay itself still installs only `websockets`. This
runs when events.json changes, which is the moment firmware and Mac can drift
apart. Run it before touching that file.
"""

import json
import os
import sys

try:
    from jsonschema import Draft202012Validator
except ImportError:
    sys.exit("needs jsonschema:  pip3 install jsonschema")

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "events.json")) as f:
    SCHEMA = json.load(f)
TOP = Draft202012Validator(SCHEMA)


def one(name):
    """Validator for a single $def, so a failure names the message instead of
    reporting 'nothing in oneOf matched'."""
    return Draft202012Validator({**SCHEMA["$defs"][name], "$defs": SCHEMA["$defs"]})


# (def name, payload, should_pass, why it matters)
CASES = []
for name, defn in SCHEMA["$defs"].items():
    for ex in defn.get("examples", []):
        CASES.append((name, ex, True, "documented example"))

CASES += [
    # The whole reason `v` exists.
    ("listen", {"v": 2, "type": "listen"}, False,
     "a version bump must fail loudly, not get parsed as v1"),
    ("listen", {"type": "listen"}, False, "v is not optional"),

    # additionalProperties:false throughout. Firmware with 197 KB of RAM should
    # not be guessing what an unexpected field meant.
    ("listen", {"v": 1, "type": "listen", "extra": 1}, False,
     "an unknown field is a contract break, not something to ignore"),
    ("ack", {"v": 1, "type": "ack", "id": "b_reply", "state": "played"}, False,
     "state was dropped - an ack that exists already means it played"),

    # The ack is what clears the Mac's queue, so a bare ack would clear
    # something nobody heard.
    ("ack", {"v": 1, "type": "ack"}, False, "an ack must say what it played"),
    ("audio_retry", {"v": 1, "type": "audio.retry"}, False,
     "a retry must identify the exact in-flight span it restores"),
    ("dismiss", {"v": 1, "type": "dismiss"}, False,
     "dismissal must identify the exact staged snapshot"),

    # Answer-expectation metadata remains explicit even though all unread
    # speech now uses the same yellow Device light.
    ("digest", {"v": 1, "type": "digest", "missed": 1,
                "ts": "2026-08-06T18:04:11Z"}, False,
     "needs_answer is required so the transport cannot silently lose whether "
     "the Agent expects the next turn to answer its question"),
    ("digest", {"v": 1, "type": "digest", "missed": -1, "needs_answer": False,
                "ts": "2026-08-06T18:04:11Z"}, False, "a negative backlog is a bug"),
    ("digest", {"v": 1, "type": "digest", "missed": 1, "needs_answer": False,
                "summary": "x" * 201, "ts": "2026-08-06T18:04:11Z"}, False,
     "summary is bounded so a runaway agent cannot send a novel"),
    ("digest", {"v": 1, "type": "digest", "missed": 1, "needs_answer": False,
                "dismissible": "yes", "ts": "2026-08-06T18:04:11Z"}, False,
     "dismissible is an exact capability, not a truthy label"),

    # Fields that were real in the OLED and triage era and are not any more.
    ("digest", {"v": 1, "type": "digest", "missed": 1, "needs_answer": False,
                "high": 0, "ts": "2026-08-06T18:04:11Z"}, False,
     "high is gone - there is no triage and no second LED colour to drive"),
    ("digest", {"v": 1, "type": "digest", "missed": 1, "needs_answer": False,
                "filtered": 0, "ts": "2026-08-06T18:04:11Z"}, False,
     "filtered is gone with the triage pipeline"),

    ("audio_span", {"v": 1, "type": "audio.start"}, False,
     "a span without an id cannot be acked"),
    ("audio_span", {"v": 1, "type": "audio.middle", "id": "b_reply"}, False,
     "there are two audio frames, not three"),
    ("audio_span", {"v": 1, "type": "audio.end", "id": "b_reply",
                    "complete": "false"}, False,
     "an interrupted span flag must be boolean, not a truthy string"),

    ("status", {"v": 1, "type": "status", "battery_pct": 101}, False,
     "battery is a percentage"),
    ("status", {"v": 1, "type": "status", "rssi": 40}, False,
     "rssi is dBm and negative - a positive one means the units are wrong"),

    ("hello", {"v": 1, "type": "hello", "autoplay": "false"}, False,
     "the string 'false' is truthy in most languages, so it must not validate"),
    ("hello", {"v": 1, "type": "hello", "autoplay": False,
               "preload": "true"}, False,
     "preload is a negotiated boolean capability, not a truthy string"),
    ("peer", {"v": 1, "type": "peer"}, False,
     "an absent Mac state must not be guessed from the relay socket"),
    ("peer", {"v": 1, "type": "peer", "mac": "false"}, False,
     "the string 'false' is truthy and would display a dead Mac as live"),
    ("presence", {"v": 1, "type": "presence", "cloud": True,
                  "link": True, "agent": "ready"}, True,
     "cloud, Link, and Agent availability are separate facts"),
    ("presence", {"v": 1, "type": "presence", "cloud": True,
                  "link": True, "agent": "unknown"}, False,
     "Agent availability is a bounded state"),
    ("muse_pair_complete", {"v": 1, "type": "muse.pair.complete",
                            "access_token": "a", "refresh_token": "r",
                            "noise_host": "hatch.metaaivm.com",
                            "mac": "88:56:A6:AC:9A:54"}, False,
     "the gadget name is derived from a lowercase MAC; a second spelling would "
     "name a different gadget"),
    ("muse_pair_complete", {"v": 1, "type": "muse.pair.complete",
                            "access_token": "a", "refresh_token": "r",
                            "noise_host": "hatch.metaaivm.com",
                            "mac": "88:56:a6:ac:9a:54", "sdk_token": "x"}, False,
     "the Device never echoes the SDK token back to the cloud"),
    ("muse_pair_open", {"v": 1, "type": "muse.pair.open"}, False,
     "the Device must not open Bluetooth on an ambiguous answer"),

    # Firmware reporting and OTA targets.
    ("firmware_status", {"v": 1, "type": "firmware.status", "revision": 2,
                         "version": "1.1.0", "state": "retrying", "attempt": 3,
                         "retry_after_s": 1200}, True,
     "retry progress carries a target revision and delay"),
    ("firmware_status", {"v": 1, "type": "firmware.status", "revision": 2,
                         "version": "1.1.0", "state": "retrying"}, False,
     "a retry needs an explicit delay"),
    ("firmware_status", {"v": 1, "type": "firmware.status", "revision": 2,
                         "version": "1.1.0", "state": "installed", "retry_after_s": 10}, False,
     "an installed report has no retry delay"),
    ("firmware_status", {"v": 1, "type": "firmware.status", "revision": -1,
                         "version": "1.1.0", "state": "accepted"}, False,
     "Device reports cannot acknowledge an unknown catalog revision"),
    ("firmware_available", {"v": 1, "type": "firmware.available", "revision": 3,
                            "version": "1.0.0"}, True,
     "withdrawals carry an ordered revision"),
    ("firmware_available", {"v": 1, "type": "firmware.available", "revision": -1,
                            "version": "1.0.0"}, False,
     "target revisions must be non-negative"),
    ("firmware_available", {"v": 1, "type": "firmware.available", "version": "1.0.0",
                            "size": 1024}, False,
     "partial image metadata must never look like a withdrawal"),
    ("hello", {"v": 1, "type": "hello", "preload": True, "board": "06", "firmware": "1.0.0"}, True, "a Device reports its board and running firmware"),
    ("hello", {"v": 1, "type": "hello", "preload": True, "board": "06", "firmware": "1.0.0", "firmware_failed": "1.1.0"}, True,
     "a Device reports the version it rejected or rolled back"),
    ("hello", {"v": 1, "type": "hello", "preload": True, "firmware": "1.0.0"}, False,
     "firmware without board cannot select an image"),
    ("hello", {"v": 1, "type": "hello", "board": "06", "firmware": "12345.0.0"}, False,
     "version parts are at most 4 digits"),
    ("hello", {"v": 1, "type": "hello", "board": "06"}, False,
     "board without firmware says nothing useful"),
    ("hello", {"v": 1, "type": "hello", "preload": True, "board": "06", "firmware": "1.0"}, False, "version is major.minor.patch"),
    ("hello", {"v": 1, "type": "hello", "preload": True, "board": "06", "firmware": "v1.0.0"}, False, "no v prefix on versions"),
    ("hello", {"v": 1, "type": "hello", "preload": True, "board": "06_A", "firmware": "1.0.0"}, False, "board is lowercase slug"),
    ("hello", {"v": 1, "type": "hello", "board": "06", "firmware_failed": "1.1.0"}, False,
     "firmware_failed needs the running firmware"),
    ("hello", {"v": 1, "type": "hello", "updates": "paused"}, False,
     "updates is auto or manual"),
    ("firmware_available", {"v": 1, "type": "firmware.available", "version": "1.0.0"}, True,
     "withdraw form: version only, no update"),
    ("firmware_available", {"v": 1, "type": "firmware.available", "version": "1.1.0", "url": "https://example.com/fw.bin", "size": 1024, "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "signature": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="}, True,
     "a full target carries all four image fields"),
    ("firmware_available", {"v": 1, "type": "firmware.available", "version": "1.1.0",
                            "url": "https://example.com/fw.bin", "size": 1024,
                            "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}, False,
     "an image without a signature must not be installable"),
    ("firmware_available", {"v": 1, "type": "firmware.available", "version": "1.1.0", "url": "https://example.com/fw.bin", "size": 4194305, "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "signature": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="}, False,
     "image larger than the OTA partition"),
    ("firmware_available", {"v": 1, "type": "firmware.available", "version": "1.1.0", "url": "https://example.com/fw.bin", "size": 1024, "sha256": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "signature": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="}, False,
     "sha256 is lowercase hex"),
    ("firmware_available", {"v": 1, "type": "firmware.available", "version": "1.1.0", "url": "http://example.com/fw.bin", "size": 1024, "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "signature": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=="}, False,
     "image URL must be https"),
    ("firmware_available", {"v": 1, "type": "firmware.available", "version": "1.1.0", "extra": 1}, False,
     "an unknown field is a contract break"),
]


def main():
    bad = 0
    for name, payload, should_pass, why in CASES:
        errs = sorted(one(name).iter_errors(payload), key=lambda e: e.path)
        passed = not errs
        if passed != should_pass:
            bad += 1
            print(f"FAIL  {name}: expected {'pass' if should_pass else 'fail'} "
                  f"- {why}\n      {json.dumps(payload)}")
            if errs:
                print(f"      {errs[0].message}")
        # Anything valid for one $def must also be valid against the top-level
        # oneOf, or a device validating the whole contract would reject it.
        if should_pass and passed and list(TOP.iter_errors(payload)):
            bad += 1
            print(f"FAIL  {name}: passes alone but not against the top-level "
                  f"oneOf\n      {json.dumps(payload)}")

    print(f"{'ok' if not bad else 'FAILED'}  ({len(CASES)} payloads, {bad} wrong)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
