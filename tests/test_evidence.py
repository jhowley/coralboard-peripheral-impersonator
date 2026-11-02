import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from coralbench import evidence as E  # noqa: E402

META = {"profile_id": "bench-temp-raw-v1", "profile_digest": "a" * 64,
        "label": "unit test session"}


def txs():
    return [E.Transaction(0, 0x48, b"\x01", None),
            E.Transaction(1, 0x48, b"\x01", b"\x60\x4b")]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "session.jsonl")

    def raw_lines(self):
        with open(self.path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def rewrite(self, lines):
        with open(self.path, "w", encoding="utf-8") as fh:
            for obj in lines:
                fh.write(json.dumps(obj) + "\n")


class TestRoundTrip(Base):
    def test_write_then_read(self):
        info = E.write_session(self.path, META, txs())
        self.assertEqual(info["transactions"], 2)
        s = E.read_session(self.path)
        self.assertEqual(s.meta["profile_id"], "bench-temp-raw-v1")
        self.assertEqual(len(s.transactions), 2)
        self.assertEqual(s.transactions[1].r, b"\x60\x4b")
        self.assertIsNone(s.transactions[0].r)
        self.assertEqual(s.raw_tx_digest, E.tx_digest(txs()))

    def test_file_is_byte_level_jsonl(self):
        E.write_session(self.path, META, txs())
        with open(self.path, encoding="utf-8") as fh:
            first = fh.readline()
        header = json.loads(first)
        self.assertEqual(header["type"], "header")
        self.assertEqual(header["schema"], E.SCHEMA)
        self.assertEqual(header["source"], E.SOURCE_SIMULATION)
        self.assertEqual(header["label"], E.SIMULATION_LABEL)
        lines = self.raw_lines()
        self.assertEqual(lines[1]["w"], "01")
        self.assertEqual(lines[1]["r"], None)
        self.assertEqual(lines[3]["type"], "footer")
        self.assertEqual(lines[3]["tx_sha256"], info_digest(self.path))

    def test_digest_is_stable_for_same_transactions(self):
        self.assertEqual(E.tx_digest(txs()), E.tx_digest(txs()))


def info_digest(path):
    with open(path, encoding="utf-8") as fh:
        lines = [json.loads(x) for x in fh if x.strip()]
    return lines[-1]["tx_sha256"]


class TestRejection(Base):
    def test_tampered_reads_are_detected(self):
        E.write_session(self.path, META, txs())
        lines = self.raw_lines()
        lines[2]["r"] = "ffff"
        self.rewrite(lines)
        with self.assertRaises(E.EvidenceError) as ctx:
            E.read_session(self.path)
        self.assertIn("digest", str(ctx.exception))

    def test_missing_footer(self):
        E.write_session(self.path, META, txs())
        lines = self.raw_lines()[:-1]
        self.rewrite(lines)
        with self.assertRaises(E.EvidenceError):
            E.read_session(self.path)

    def test_bad_json_reports_line_number(self):
        E.write_session(self.path, META, txs())
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        with self.assertRaises(E.EvidenceError) as ctx:
            E.read_session(self.path)
        self.assertEqual(ctx.exception.line_no, 5)

    def test_uppercase_hex_rejected(self):
        E.write_session(self.path, META, txs())
        lines = self.raw_lines()
        lines[2]["r"] = "60AB"
        self.rewrite(lines)
        with self.assertRaises(E.EvidenceError) as ctx:
            E.read_session(self.path)
        self.assertIn("hex", str(ctx.exception))

    def test_unknown_record_type(self):
        E.write_session(self.path, META, txs())
        lines = self.raw_lines()
        lines.insert(1, {"type": "mystery"})
        self.rewrite(lines)
        with self.assertRaises(E.EvidenceError):
            E.read_session(self.path)

    def test_hardware_source_requires_device(self):
        with self.assertRaises(E.EvidenceError):
            E.write_session(self.path, META, txs(), source=E.SOURCE_HARDWARE)
        info = E.write_session(self.path, META, txs(), source=E.SOURCE_HARDWARE,
                               device="/dev/ttyACM-test")
        self.assertEqual(info["source"], E.SOURCE_HARDWARE)

    def test_empty_session_is_rejected(self):
        with self.assertRaises(E.EvidenceError):
            E.write_session(self.path, META, [])


class TestCompleteness(Base):
    def test_incomplete_footer_is_preserved_and_reported(self):
        info = E.write_session(self.path, META, txs(), complete=False,
                               incomplete_reason="dropped events")
        self.assertFalse(info["complete"])
        s = E.read_session(self.path)
        self.assertFalse(s.complete)
        self.assertEqual(s.incomplete_reason, "dropped events")

    def test_complete_true_with_dropped_trace_is_refused(self):
        meta = dict(META, trace_dropped=5)
        with self.assertRaises(E.EvidenceError):
            E.write_session(self.path, meta, txs(), complete=True)

    def test_default_verdict_is_derived_from_trace_metadata(self):
        info = E.write_session(self.path, dict(META, trace_dropped=5), txs())
        self.assertFalse(info["complete"])
        self.assertFalse(E.read_session(self.path).complete)

    def test_read_rejects_complete_footer_over_dropped_meta(self):
        E.write_session(self.path, dict(META, trace_dropped=5), txs(),
                        complete=False, incomplete_reason="dropped")
        lines = self.raw_lines()
        lines[-1]["complete"] = True
        self.rewrite(lines)
        with self.assertRaises(E.EvidenceError):
            E.read_session(self.path)


class TestCampaign(Base):
    def events(self):
        return [
            {"type": "trace", "epoch": 0x00010002, "seq": 0, "op": "w",
             "ptr": 1, "len": 0, "data": "", "trunc": 0},
            {"type": "trace", "epoch": 0x00010002, "seq": 1, "op": "r",
             "ptr": 1, "len": 2, "data": "604b", "trunc": 0},
        ]

    def meta(self):
        return {"profile_id": "bench-temp-raw-v1", "profile_digest": "a" * 64,
                "config_crc": 0x12345678, "epoch": 0x00010002,
                "emitted": 2, "dropped": 0, "aborted": 0, "drained": 2}

    def test_campaign_round_trip(self):
        info = E.write_campaign(self.path, self.meta(), self.events(),
                                source=E.SOURCE_HARDWARE, device="/dev/ttyACM-test",
                                complete=True)
        self.assertEqual(info["events"], 2)
        c = E.read_campaign(self.path)
        self.assertTrue(c.complete)
        self.assertEqual(c.meta["epoch"], 0x00010002)
        self.assertEqual([e["seq"] for e in c.events], [0, 1])

    def test_complete_campaign_rejects_missing_sequence(self):
        events = self.events()[1:]
        with self.assertRaises(E.EvidenceError):
            E.write_campaign(self.path, self.meta(), events,
                             source=E.SOURCE_HARDWARE, device="/dev/ttyACM-test",
                             complete=True)

    def test_complete_campaign_rejects_abort_and_truncation(self):
        meta = dict(self.meta(), aborted=1)
        with self.assertRaises(E.EvidenceError):
            E.write_campaign(self.path, meta, self.events(),
                             source=E.SOURCE_HARDWARE, device="/dev/ttyACM-test",
                             complete=True)
        events = self.events()
        events[1] = dict(events[1], trunc=1)
        with self.assertRaises(E.EvidenceError):
            E.write_campaign(self.path, self.meta(), events,
                             source=E.SOURCE_HARDWARE, device="/dev/ttyACM-test",
                             complete=True)

    def test_campaign_schema_is_not_a_session(self):
        E.write_campaign(self.path, self.meta(), self.events(),
                         source=E.SOURCE_HARDWARE, device="/dev/ttyACM-test",
                         complete=True)
        with self.assertRaises(E.EvidenceError):
            E.read_session(self.path)


if __name__ == "__main__":
    unittest.main()
