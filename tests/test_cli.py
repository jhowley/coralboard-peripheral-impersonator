"""CLI exercised as a real subprocess (no hardware).

`serial` is driven over a pty pair whose far end is the labelled fake device
from tests/support/fake_pico.py; that is a test double, not the firmware.
"""

import json
import os
import pty
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"))
GOOD = os.path.join(ROOT, "profiles", "bench-temp-raw-v1.json")
OPS = os.path.join(ROOT, "tests", "fixtures", "ops-smoke.json")


def run(*args, check=None):
    proc = subprocess.run([sys.executable, "-m", "coralbench", *args],
                          cwd=ROOT, env=ENV, capture_output=True, text=True,
                          timeout=60)
    if check is not None:
        if proc.returncode != check:
            raise AssertionError(
                f"exit={proc.returncode} expected={check}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return proc


class TestValidate(unittest.TestCase):
    def test_valid_profile(self):
        proc = run("validate", GOOD, check=0)
        self.assertIn("OK validate", proc.stdout)
        self.assertIn("bench-temp-raw-v1", proc.stdout)
        self.assertIn("0x48", proc.stdout)

    def test_bad_profile_reports_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = json.load(open(GOOD))
            doc["fidelity"] = "faithful-tmp102"
            path = os.path.join(tmp, "bad.json")
            json.dump(doc, open(path, "w"))
            proc = run("validate", path, check=1)
            self.assertIn("fidelity", proc.stderr)

    def test_missing_file(self):
        proc = run("validate", "/nonexistent/profile.json", check=1)
        self.assertIn("Error", proc.stderr)


class TestSimulateReplay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "evidence.jsonl")

    def test_simulate_writes_labelled_evidence(self):
        proc = run("simulate", "--profile", GOOD, "--ops", OPS, "--out", self.out, check=0)
        self.assertIn("SIMULATED", proc.stdout)
        self.assertTrue(os.path.exists(self.out))
        lines = [json.loads(l) for l in open(self.out) if l.strip()]
        self.assertEqual(lines[0]["type"], "header")
        self.assertEqual(lines[0]["source"], "simulation")
        self.assertIn("SIMULATED", lines[0]["label"])
        tx = [l for l in lines if l["type"] == "tx"]
        self.assertEqual(len(tx), 7)
        self.assertEqual(tx[1]["r"], "19")
        self.assertEqual(tx[2]["r"], "604b50")

    def test_replay_passes(self):
        run("simulate", "--profile", GOOD, "--ops", OPS, "--out", self.out, check=0)
        proc = run("replay", "--profile", GOOD, "--evidence", self.out, check=0)
        self.assertIn("PASS", proc.stdout)
        self.assertIn("tx=7", proc.stdout)

    def test_replay_detects_tampering(self):
        run("simulate", "--profile", GOOD, "--ops", OPS, "--out", self.out, check=0)
        lines = [json.loads(l) for l in open(self.out) if l.strip()]
        for rec in lines:
            if rec["type"] == "tx" and rec["r"]:
                rec["r"] = "00"
                break
        with open(self.out, "w") as fh:
            for rec in lines:
                fh.write(json.dumps(rec) + "\n")
        proc = run("replay", "--profile", GOOD, "--evidence", self.out)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("digest", (proc.stdout + proc.stderr).lower())

    def test_simulate_refuses_missing_ops(self):
        proc = run("simulate", "--profile", GOOD, "--ops", "/nonexistent.json",
                   "--out", self.out, check=1)
        self.assertIn("Error", proc.stderr)

    def test_overflowed_simulation_is_written_incomplete_and_not_replayable(self):
        proc = run("simulate", "--profile", GOOD, "--ops", OPS,
                   "--out", self.out, "--queue-limit", "2", check=1)
        self.assertIn("INCOMPLETE", proc.stdout)
        lines = [json.loads(line) for line in open(self.out) if line.strip()]
        self.assertFalse(lines[-1]["complete"])
        proc = run("replay", "--profile", GOOD, "--evidence", self.out)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("evidence_incomplete", proc.stdout)


def pty_device_for(fake):
    """Serve `fake` over a pty and return (path, stop callable)."""
    master, slave = pty.openpty()
    stop = threading.Event()
    path = os.ttyname(slave)

    def pump():
        buf = b""
        while not stop.is_set():
            import select
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


class TestSerialCli(unittest.TestCase):
    def test_serial_status_over_pty_and_fake_device(self):
        sys.path.insert(0, os.path.join(ROOT, "src"))
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        from coralbench import profile as P
        from support.fake_pico import FakePicoTransport

        fake = FakePicoTransport(P.load_profile(GOOD))
        path, cleanup = pty_device_for(fake)
        self.addCleanup(cleanup)
        time.sleep(0.1)
        proc = run("serial", "--device", path, "status", check=0)
        self.assertIn("loaded=0", proc.stdout)
        self.assertIn("addr=0x48", proc.stdout)

    def test_serial_refuses_glob_and_missing_device_before_writing(self):
        proc = run("serial", "--device", "/dev/ttyACM*", "status", check=1)
        self.assertIn("glob", proc.stderr)
        proc = run("serial", "--device", "/dev/tty-does-not-exist", "status", check=1)
        self.assertIn("Error", proc.stderr)

    def test_serial_arm_requires_profile_and_bounded_lease(self):
        sys.path.insert(0, os.path.join(ROOT, "src"))
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        from coralbench import profile as P
        from support.fake_pico import FakePicoTransport

        fake = FakePicoTransport(P.load_profile(GOOD))
        path, cleanup = pty_device_for(fake)
        self.addCleanup(cleanup)
        time.sleep(0.1)
        proc = run("serial", "--device", path, "arm", "--lease-ms", "12000", check=1)
        self.assertIn("lease", (proc.stdout + proc.stderr).lower())
        self.assertEqual([c for c in fake.commands if c.startswith("ARM")], [])

    def test_serial_collect_writes_raw_campaign_evidence(self):
        sys.path.insert(0, os.path.join(ROOT, "src"))
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        from coralbench import profile as P
        from support.fake_pico import FakePicoTransport

        fake = FakePicoTransport(P.load_profile(GOOD))
        path, cleanup = pty_device_for(fake)
        self.addCleanup(cleanup)
        time.sleep(0.1)
        run("serial", "--device", path, "load", "--profile", GOOD, check=0)
        run("serial", "--device", path, "arm", "--lease-ms", "1000", check=0)
        fake.target.read(2)
        run("serial", "--device", path, "disarm", check=0)
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "campaign.jsonl")
            proc = run("serial", "--device", path, "collect",
                       "--profile", GOOD, "--out", out, check=0)
            self.assertIn("campaign complete", proc.stdout)
            self.assertIn("raw campaign evidence", proc.stdout)
            lines = [json.loads(line) for line in open(out) if line.strip()]
            self.assertEqual(lines[0]["schema"], "coralboard-bench/campaign/v1")
            self.assertEqual(lines[0]["meta"]["epoch"], lines[1]["epoch"])
            self.assertTrue(lines[-1]["complete"])
            trace = [r for r in lines if r.get("type") == "trace"]
            self.assertEqual([r["seq"] for r in trace], [0])


if __name__ == "__main__":
    unittest.main()
