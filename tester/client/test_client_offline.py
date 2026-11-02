#!/usr/bin/env python3
"""Offline client tests: no device, no serial port, no libusb.

Verifies the identity gate and that the default action never sends a bus
command. Run: python3 client/test_client_offline.py
"""

import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tester_client as tc  # noqa: E402

failures = 0


class FakeLink:
    path = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    def send_line(self, line):
        self.sent.append(line)

    def read_line(self):
        return self.replies.pop(0) if self.replies else None

    def close(self):
        pass


def run(args, replies, expect_link=True):
    global failures
    fake = FakeLink(replies)
    called = {"n": 0}

    def fake_open(_args):
        called["n"] += 1
        if not expect_link:
            raise AssertionError("link must not be opened for invalid args")
        return fake

    old_argv, old_open = sys.argv, tc.open_link
    sys.argv = ["tester_client.py", "--serial", "E660000000000001"] + args
    tc.open_link = fake_open
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = tc.main()
    finally:
        sys.argv, tc.open_link = old_argv, old_open
    return rc, fake.sent, called["n"]


PING = "OK PING TESTER fw=1.0.0"
STATUS = ("OK STATUS role=TESTER fw=1.0.0 addr=0x48 i2c=0 sda=4 scl=5 "
          "hz=100000 pins=input pulls=off ops=0 ok=0 err=0 timeouts=0 last=none")

cases = []

rc, sent, _ = run([], [PING, STATUS])
cases.append(("default PING+STATUS only", rc == 0 and sent == ["PING", "STATUS"],
              f"rc={rc} sent={sent}"))

rc, sent, _ = run([], ["OK PING m1-i2c fw=0.2.0"])
cases.append(("sensor PING refused before bus", rc == 2 and sent == ["PING"],
              f"rc={rc} sent={sent}"))

rc, sent, _ = run([], ["OK PING TESTER fw=2.0.0"])
cases.append(("version mismatch refused", rc == 2 and sent == ["PING"],
              f"rc={rc} sent={sent}"))

rc, sent, _ = run(["--read", "4"], [PING, STATUS,
                                    "OK R n=4 data=19604b50 fw=1.0.0"])
cases.append(("one explicit read", rc == 0 and sent == ["PING", "STATUS", "R 4"],
              f"rc={rc} sent={sent}"))

rc, sent, _ = run(["--write-read", "00", "4"],
                  [PING, STATUS, "OK WR wn=1 rn=4 data=19604b50 fw=1.0.0"])
cases.append(("one explicit write-read",
              rc == 0 and sent == ["PING", "STATUS", "WR 00 4"],
              f"rc={rc} sent={sent}"))

rc, sent, opened = run(["--write-read", "0", "4"], [], expect_link=False)
cases.append(("invalid hex never opens a link", rc == 2 and opened == 0,
              f"rc={rc} opened={opened}"))

for name, ok, detail in cases:
    if not ok:
        failures += 1
        print(f"FAIL {name}: {detail}")
    else:
        print(f"PASS {name}")

print(f"{len(cases)} checks, {failures} failures")
sys.exit(1 if failures else 0)
