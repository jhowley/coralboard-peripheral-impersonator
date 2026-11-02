"""Regression tests for previously reported simulator/core/evidence defects.

These pin the five review findings and the streaming-collect CLI requirement:

* B2  the simulator must advance/wrap per byte exactly like the C core,
      including auto_increment=false, wrap=1, wrap=256 and a 256-byte read.
* B5  the evidence readers must never certify an empty or unverifiable
      session/campaign as complete, and a write-only replay must be
      INCONCLUSIVE unless a final image (or equivalent) is supplied.
* B1  firmware-only: the matching-epoch alarm guard is covered by
      firmware/host_test/test_core.c.
* Streaming `serial collect --out` while armed: the controller/CLI must drain
      the same epoch until the lease seals, then validate and save.

No hardware and no network are used here.
"""

from __future__ import annotations

import hashlib
import json
import os
import pty
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from coralbench import controller as C  # noqa: E402
from coralbench import evidence as E  # noqa: E402
from coralbench import profile as P  # noqa: E402
from coralbench import replay as R  # noqa: E402
from coralbench import simulator as S  # noqa: E402
from support.fake_pico import FakePicoTransport, _Clock  # noqa: E402

GOOD = os.path.join(ROOT, "profiles", "bench-temp-raw-v1.json")
ALT = os.path.join(ROOT, "profiles", "bench-temp-alt-v1.json")


# --------------------------------------------------------------- B2 ---
class TestSimulatorByteWrap(unittest.TestCase):
    """The simulator must match core.c's per-byte pointer advance."""

    def setUp(self):
        self.profile = P.load_profile(GOOD)  # auto_increment, wrap=4
        self.target = S.SimulatedTarget(self.profile)
        self.target.arm()

    def test_read_wraps_per_byte_at_pointer_wrap(self):
        # image = 19 60 4b 50, pointer 2, read 6 => 4b 50 19 60 4b 50
        self.target.write(b"\x02")
        self.assertEqual(self.target.read(6), b"\x4b\x50\x19\x60\x4b\x50")
        self.assertEqual(self.target.pointer, (2 + 6) % 4)

    def test_wrap_one_repeats_the_same_byte(self):
        profile = P.load_profile(ALT)
        profile = _replace_profile(profile, pointer_wrap=1, pointer_initial=0,
                                   auto_increment=True)
        t = S.SimulatedTarget(profile)
        t.arm()
        self.assertEqual(t.read(3), b"\x15\x15\x15")
        self.assertEqual(t.pointer, 0)

    def test_wrap_256_crosses_the_end_of_the_image(self):
        profile = _replace_profile(self.profile, pointer_wrap=256,
                                   pointer_initial=255)
        t = S.SimulatedTarget(profile)
        t.arm()
        # pointer byte 0xff stays 255 under wrap=256
        t.write(b"\xff")
        self.assertEqual(t.read(2), b"\x00\x19")
        self.assertEqual(t.pointer, 1)

    def test_256_byte_read_is_full_image_in_order(self):
        profile = _replace_profile(self.profile, pointer_wrap=256,
                                   pointer_initial=0)
        t = S.SimulatedTarget(profile)
        t.arm()
        data = bytes(t.image)  # expected: full image from pointer 0
        t.write(b"\x00")
        self.assertEqual(t.read(256), data)
        self.assertEqual(t.pointer, 0)

    def test_no_auto_increment_repeats_the_pointer_byte(self):
        profile = P.load_profile(ALT)  # auto_increment=false, wrap=4
        t = S.SimulatedTarget(profile)
        t.arm()
        t.write(b"\x02")
        self.assertEqual(t.read(3), b"\x40\x40\x40")
        self.assertEqual(t.pointer, 2)


def _replace_profile(profile, **kwargs):
    import dataclasses
    return dataclasses.replace(profile, **kwargs)


# --------------------------------------------------------------- B5 ---
def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _write_raw_session(path, meta, txs, complete=True, reason=None):
    header = {"type": "header", "schema": E.SCHEMA, "source": "simulation",
              "label": E.SIMULATION_LABEL, "device": None,
              "created_utc": "2026-01-01T00:00:00Z", "meta": meta, "notes": []}
    footer = {"type": "footer", "schema": E.SCHEMA, "transactions": len(txs),
              "tx_sha256": E.tx_digest(txs), "complete": complete}
    if not complete:
        footer["incomplete_reason"] = reason or "test"
    lines = [_canonical(header)]
    lines.extend(_canonical(tx.as_dict()) for tx in txs)
    lines.append(_canonical(footer))
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


def _write_raw_campaign(path, meta, events, complete=True, reason=None):
    header = {"type": "header", "schema": E.CAMPAIGN_SCHEMA,
              "source": "hardware", "label": E.CAMPAIGN_HW_LABEL,
              "device": "/dev/ttyTEST", "created_utc": "2026-01-01T00:00:00Z",
              "meta": meta, "notes": []}
    footer = {"type": "footer", "schema": E.CAMPAIGN_SCHEMA,
              "events": len(events), "events_sha256": E.campaign_digest(events),
              "complete": complete}
    if not complete:
        footer["incomplete_reason"] = reason or "test"
    lines = [_canonical(header)]
    lines.extend(_canonical(ev) for ev in events)
    lines.append(_canonical(footer))
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


def _campaign_meta(epoch=0x00010002, emitted=2, dropped=0, aborted=0,
                   drained=2):
    return {"profile_id": "bench-temp-raw-v1", "profile_digest": "a" * 64,
            "config_crc": 0x12345678, "epoch": epoch, "emitted": emitted,
            "dropped": dropped, "aborted": aborted, "drained": drained}


def _campaign_events(epoch=0x00010002):
    return [
        {"type": "trace", "epoch": epoch, "seq": 0, "op": "w", "ptr": 1,
         "len": 0, "data": "", "trunc": 0},
        {"type": "trace", "epoch": epoch, "seq": 1, "op": "r", "ptr": 1,
         "len": 2, "data": "604b", "trunc": 0},
    ]


class TestEvidenceRejectsFalseCompleteness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "e.jsonl")

    def test_empty_complete_session_is_rejected(self):
        _write_raw_session(self.path,
                           {"profile_id": "bench-temp-raw-v1",
                            "profile_digest": "a" * 64}, [], complete=True)
        with self.assertRaises(E.EvidenceError):
            E.read_session(self.path)

    def test_empty_complete_campaign_is_rejected(self):
        _write_raw_campaign(self.path, _campaign_meta(emitted=0, drained=0),
                            [], complete=True)
        with self.assertRaises(E.EvidenceError):
            E.read_campaign(self.path)

    def test_complete_campaign_over_dropped_meta_is_rejected(self):
        _write_raw_campaign(self.path, _campaign_meta(dropped=2),
                            _campaign_events(), complete=True)
        with self.assertRaises(E.EvidenceError):
            E.read_campaign(self.path)

    def test_complete_campaign_with_stale_event_epoch_is_rejected(self):
        meta = _campaign_meta()
        events = _campaign_events(epoch=0x00010001)
        _write_raw_campaign(self.path, meta, events, complete=True)
        with self.assertRaises(E.EvidenceError):
            E.read_campaign(self.path)

    def test_campaign_without_explicit_drained_is_rejected(self):
        meta = _campaign_meta()
        meta.pop("drained")
        with self.assertRaises(E.EvidenceError):
            E.write_campaign(self.path, meta, _campaign_events(),
                             source=E.SOURCE_HARDWARE, device="/dev/ttyTEST",
                             complete=True)

    def test_campaign_event_data_length_must_match_len(self):
        events = _campaign_events()
        events[1] = dict(events[1], data="60")
        _write_raw_campaign(self.path, _campaign_meta(), events, complete=True)
        with self.assertRaises(E.EvidenceError):
            E.read_campaign(self.path)

    def test_complete_write_only_session_requires_final_image(self):
        tx = E.Transaction(0, 0x48, b"\x01\x99", None)
        with self.assertRaises(E.EvidenceError):
            E.write_session(self.path, {"profile_id": "bench-temp-raw-v1",
                                        "profile_digest": "a" * 64},
                            [tx], complete=True)

    def test_read_rejects_write_only_session_without_image(self):
        tx = E.Transaction(0, 0x48, b"\x01\x99", None)
        _write_raw_session(self.path,
                           {"profile_id": "bench-temp-raw-v1",
                            "profile_digest": P.load_profile(GOOD).digest},
                           [tx], complete=True)
        with self.assertRaises(E.EvidenceError):
            E.read_session(self.path)

    def test_replay_write_only_without_final_image_is_inconclusive(self):
        # A pre-rule/legacy Session object (not read through the strict reader)
        # must still never produce a tidy PASS.
        profile = P.load_profile(GOOD)
        tx = E.Transaction(0, 0x48, b"\x01\x99", None)
        session = E.Session(meta={"profile_id": profile.profile_id,
                                  "profile_digest": profile.digest},
                            transactions=[tx], raw_tx_digest=E.tx_digest([tx]),
                            complete=True)
        result = R.replay_session(session, profile)
        self.assertFalse(result.ok)
        self.assertEqual(result.summary().split()[1], "INCONCLUSIVE")

    def test_replay_write_only_with_final_image_passes(self):
        profile = P.load_profile(GOOD)
        target = S.SimulatedTarget(profile)
        target.arm()
        target.write(b"\x01\x99")
        final = hashlib.sha256(bytes(target.image)).hexdigest()
        target.disarm()
        tx = E.Transaction(0, 0x48, b"\x01\x99", None)
        _write_raw_session(self.path,
                           {"profile_id": profile.profile_id,
                            "profile_digest": profile.digest,
                            "final_image_sha256": final}, [tx], complete=True)
        result = R.replay_session(E.read_session(self.path), profile)
        self.assertTrue(result.ok, result.summary())
        self.assertIn("final_image", result.checks)


# ------------------------------------------------- streaming collect ---
class TestStreamingCollect(unittest.TestCase):
    def setUp(self):
        self.profile = P.load_profile(GOOD)
        self.clock = _Clock()
        self.fake = FakePicoTransport(self.profile, clock=self.clock,
                                      lease_ceiling_ms=10000)
        self.ctl = C.PicoController(self.fake, clock=self.clock)
        self.addCleanup(self.ctl.close)
        self.ctl.load_image(self.profile)

    def test_collect_streams_until_lease_seals(self):
        self.ctl.arm(100)
        self.fake.target.read(2)
        camp = self.ctl.collect_live(
            self.profile, timeout_s=5.0, max_events=64,
            sleeper=lambda s: self.clock.advance(0.2))
        self.assertEqual(len(camp.events), 1)
        self.assertEqual(camp.events[0].seq, 0)
        self.assertTrue(camp.complete)
        self.assertFalse(camp.status.armed)
        self.assertTrue(camp.status.sealed)
        self.assertEqual(camp.status.trace_drained, 1)

    def test_collect_after_partial_manual_drain_is_refused(self):
        self.ctl.arm(5000)
        self.fake.target.read(1)
        self.ctl.drain()
        with self.assertRaises(C.TraceIncomplete):
            self.ctl.collect_live(self.profile, timeout_s=5.0,
                                  sleeper=lambda s: None)

    def test_collect_timeout_fails_closed(self):
        self.ctl.arm(5000)
        self.fake.target.read(1)
        with self.assertRaises(C.TraceIncomplete):
            self.ctl.collect_live(self.profile, timeout_s=0.1, max_events=64,
                                  sleeper=lambda s: self.clock.advance(0.05))

    def test_collect_event_budget_fails_closed(self):
        self.ctl.arm(5000)
        self.fake.target.read(1)
        self.fake.target.read(1)
        with self.assertRaises(C.TraceIncomplete):
            self.ctl.collect_live(self.profile, timeout_s=5.0, max_events=1,
                                  sleeper=lambda s: self.clock.advance(0.01))


# ------------------------------------------------------ CLI over pty ---
ENV = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"))


def _run_cli(*args):
    return subprocess.run([sys.executable, "-m", "coralbench", *args],
                          cwd=ROOT, env=ENV, capture_output=True, text=True,
                          timeout=60)


def _pty_device_for(fake):
    master, slave = pty.openpty()
    stop = threading.Event()
    path = os.ttyname(slave)

    def pump():
        buf = b""
        while not stop.is_set():
            r, _, _ = select.select([master], [], [], 0.05)
            if not r:
                continue
            try:
                chunk = os.read(master, 512)
            except OSError:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                fake.send_line(line.decode("ascii", "replace"))
                while True:
                    reply = fake.read_line(0)
                    if reply is None:
                        break
                    os.write(master, (reply + "\n").encode())

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()

    def cleanup():
        stop.set()
        thread.join(timeout=2)
        os.close(master)
        os.close(slave)

    return path, cleanup


class TestStreamingCollectCli(unittest.TestCase):
    def test_serial_collect_while_armed_streams_and_writes_evidence(self):
        fake = FakePicoTransport(P.load_profile(GOOD), clock=time.monotonic)
        path, cleanup = _pty_device_for(fake)
        self.addCleanup(cleanup)
        time.sleep(0.1)
        proc = _run_cli("serial", "--device", path, "load", "--profile", GOOD)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = _run_cli("serial", "--device", path, "arm", "--lease-ms", "600")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        fake.target.read(2)
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "campaign.jsonl")
            proc = _run_cli("serial", "--device", path, "collect",
                            "--profile", GOOD, "--out", out,
                            "--collect-timeout", "10")
            self.assertEqual(proc.returncode, 0,
                             proc.stdout + "\n" + proc.stderr)
            self.assertIn("campaign complete", proc.stdout)
            records = [json.loads(x) for x in open(out) if x.strip()]
            self.assertTrue(records[-1]["complete"])
            traces = [r for r in records if r.get("type") == "trace"]
            self.assertEqual([r["seq"] for r in traces], [0])
            self.assertEqual(records[0]["meta"]["drained"], 1)
