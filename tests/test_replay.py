import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from coralbench import evidence as E  # noqa: E402
from coralbench import profile as P  # noqa: E402
from coralbench import replay as R  # noqa: E402

GOOD = os.path.join(ROOT, "profiles", "bench-temp-raw-v1.json")
ALT = os.path.join(ROOT, "profiles", "bench-temp-alt-v1.json")


def record(profile, ops):
    """Minimal recorder: same semantics used by the CLI simulate verb."""
    sim = __import__("coralbench.simulator", fromlist=["SimulatedTarget"])
    target = sim.SimulatedTarget(profile)
    target.arm()
    out = []
    for i, (w, n) in enumerate(ops):
        if w is not None:
            target.write(w)
            out.append(E.Transaction(i, profile.address, w, None))
        else:
            data = target.read(n)
            out.append(E.Transaction(i, profile.address, b"", data))
    target.disarm()
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "s.jsonl")
        self.profile = P.load_profile(GOOD)

    def write(self, txs=None, profile=None):
        profile = profile or self.profile
        txs = txs if txs is not None else record(profile, [(None, 1), (b"\x01", None), (None, 3)])
        meta = {"profile_id": profile.profile_id, "profile_digest": profile.digest}
        E.write_session(self.path, meta, txs)
        return E.read_session(self.path)


class TestReplay(Base):
    def test_replay_reproduces_recorded_bytes(self):
        result = R.replay_session(self.write(), self.profile)
        self.assertTrue(result.ok, result.summary())
        self.assertEqual(result.transactions, 3)
        self.assertEqual([m.seq for m in result.mismatches], [])
        self.assertIn("profile_digest", result.checks)
        self.assertIn("tx_digest", result.checks)

    def test_mismatch_is_reported_with_seq(self):
        txs = record(self.profile, [(None, 2)])
        txs[0] = E.Transaction(0, self.profile.address, b"", b"\xde\xad")
        result = R.replay_session(self.write(txs), self.profile)
        self.assertFalse(result.ok)
        self.assertEqual(result.mismatches[0].seq, 0)
        self.assertEqual(result.mismatches[0].expected, "dead")
        self.assertEqual(result.mismatches[0].actual, "1960")

    def test_wrong_profile_is_rejected(self):
        result = R.replay_session(self.write(), P.load_profile(ALT))
        self.assertFalse(result.ok)
        self.assertIn("profile_digest_mismatch", result.notes)

    def test_extra_write_changes_later_reads(self):
        txs = record(self.profile, [(None, 1), (b"\x02\x99", None), (None, 1)])
        txs[2] = E.Transaction(2, self.profile.address, b"", b"\x55")
        result = R.replay_session(self.write(txs), self.profile)
        self.assertFalse(result.ok)
        self.assertEqual(result.mismatches[0].seq, 2)
        self.assertEqual(result.mismatches[0].actual, "50")

    def test_incomplete_session_is_not_replayable(self):
        txs = record(self.profile, [(None, 1)])
        meta = {"profile_id": self.profile.profile_id,
                "profile_digest": self.profile.digest, "trace_dropped": 3}
        E.write_session(self.path, meta, txs, complete=False,
                        incomplete_reason="trace overflow")
        result = R.replay_session(E.read_session(self.path), self.profile)
        self.assertFalse(result.ok)
        self.assertIn("evidence_incomplete", result.notes)

    def test_final_image_mismatch_fails(self):
        txs = record(self.profile, [(b"\x01\x99", None), (None, 1)])
        meta = {"profile_id": self.profile.profile_id,
                "profile_digest": self.profile.digest,
                "final_image_sha256": "0" * 64}
        E.write_session(self.path, meta, txs)
        result = R.replay_session(E.read_session(self.path), self.profile)
        self.assertFalse(result.ok)
        self.assertIn("final_image_mismatch", result.notes)

    def test_address_mismatch_stops_replay_instead_of_cascading(self):
        txs = record(self.profile, [(None, 1), (None, 1)])
        txs[0] = E.Transaction(0, 0x49, b"", txs[0].r)
        meta = {"profile_id": self.profile.profile_id,
                "profile_digest": self.profile.digest}
        E.write_session(self.path, meta, txs)
        result = R.replay_session(E.read_session(self.path), self.profile)
        self.assertFalse(result.ok)
        self.assertTrue(any("addr_mismatch@0" in n for n in result.notes))
        self.assertEqual(result.mismatches, [])

    def test_rejected_write_count_is_checked(self):
        # pointer 0x10 -> 0 under wrap 4; a write outside the window is rejected.
        txs = record(self.profile, [(b"\x00\xaa", None), (None, 1)])
        meta = {"profile_id": self.profile.profile_id,
                "profile_digest": self.profile.digest, "rejected_writes": 0}
        E.write_session(self.path, meta, txs)
        result = R.replay_session(E.read_session(self.path), self.profile)
        self.assertFalse(result.ok)
        self.assertTrue(any("rejected_writes_mismatch" in n for n in result.notes))


if __name__ == "__main__":
    unittest.main()
