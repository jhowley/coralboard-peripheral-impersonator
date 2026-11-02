"""Controller tests against a FAKE transport (tests/support/fake_pico.py).

NO HARDWARE IS INVOLVED. The fake shares the simulator's semantics, so these
tests prove the host controller's parsing, bounds, drain loop and refusal
rules — not the firmware's behaviour.
"""

import dataclasses
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from coralbench import controller as C  # noqa: E402
from coralbench import profile as P  # noqa: E402
from support.fake_pico import FakePicoTransport, _Clock  # noqa: E402

GOOD = os.path.join(ROOT, "profiles", "bench-temp-raw-v1.json")


class Base(unittest.TestCase):
    queue_limit = 256

    def setUp(self):
        self.profile = P.load_profile(GOOD)
        self.clock = _Clock()
        self.fake = FakePicoTransport(self.profile, queue_limit=self.queue_limit,
                                      clock=self.clock)
        self.ctl = C.PicoController(self.fake, clock=self.clock)
        self.addCleanup(self.ctl.close)

    def load(self):
        return self.ctl.load_image(self.profile)


class TestStatusAndLoad(Base):
    def test_status_before_load(self):
        st = self.ctl.status()
        self.assertFalse(st.loaded)
        self.assertFalse(st.armed)
        self.assertEqual(st.address, 0x48)
        self.assertEqual(st.profile_tag, "none")

    def test_load_reports_crc_and_image(self):
        st = self.load()
        self.assertTrue(st.loaded)
        self.assertEqual(st.profile_tag, "bench-temp-raw-v1")
        self.assertEqual(st.image_crc, self.ctl.status().image_crc)

    def test_load_while_armed_is_refused_by_device(self):
        self.load()
        self.ctl.arm(500)
        with self.assertRaises(C.ProtocolError) as ctx:
            self.load()
        self.assertIn("ARM_ACTIVE", str(ctx.exception))

    def test_load_validates_length_locally(self):
        bad = dataclasses.replace(self.profile, image=b"\x00" * 255)
        with self.assertRaises(C.ControllerError):
            self.ctl.load_image(bad)
        self.assertEqual([c for c in self.fake.commands if c.startswith("LOAD")], [])


class TestArmBounds(Base):
    def test_arm_requires_lease_within_ceiling(self):
        self.load()
        for lease in (0, 10001, 60000, -5):
            with self.assertRaises(C.ControllerError):
                self.ctl.arm(lease)
        self.assertEqual([c for c in self.fake.commands if c.startswith("ARM")], [])

    def test_arm_requires_loaded_image(self):
        with self.assertRaises(C.ProtocolError) as ctx:
            self.ctl.arm(100)
        self.assertIn("NOT_LOADED", str(ctx.exception))

    def test_arm_then_disarm(self):
        self.load()
        st = self.ctl.arm(250)
        self.assertTrue(st.armed)
        self.assertGreater(st.lease_left_ms, 0)
        st = self.ctl.disarm()
        self.assertFalse(st.armed)
        self.assertTrue(st.sealed)

    def test_lease_expires_without_host(self):
        self.load()
        self.ctl.arm(100)
        self.clock.advance(0.101)
        st = self.ctl.status()
        self.assertFalse(st.armed)
        self.assertTrue(st.sealed)
        # Expiry is independent of the host, but a lease with no bus traffic is
        # not qualifying evidence (zero-observation rule).
        self.assertFalse(st.complete_candidate)


class TestTrace(Base):
    def test_trace_round_trip(self):
        self.load()
        self.ctl.arm(1000)
        # master traffic happens on the fake device
        self.fake.target.read(2)
        self.fake.target.write(b"\x01\x77")
        self.fake.target.read(1)
        self.ctl.disarm()
        events = self.ctl.drain()
        self.assertEqual([e.op for e in events], ["r", "w", "r"])
        self.assertEqual(events[0].data, b"\x19\x60")
        self.assertEqual([e.seq for e in events], [0, 1, 2])
        # a second, shorter lease: campaign harvest must not reuse stale counts
        self.ctl.arm(500)
        self.fake.target.read(1)
        self.ctl.disarm()
        camp = self.ctl.collect_campaign(self.profile, expect_events=1)
        self.assertTrue(camp.complete)
        self.assertEqual(len(camp.events), 1)
        self.assertEqual(camp.events[0].data, b"\x19")  # arm() resets the pointer

    def test_drain_while_armed_streams_without_completing(self):
        self.load()
        self.ctl.arm(1000)
        self.fake.target.read(1)
        events = self.ctl.drain()
        self.assertEqual(len(events), 1)
        st = self.ctl.status()
        self.assertTrue(st.armed)
        self.assertFalse(st.sealed)
        self.assertFalse(st.complete_candidate)
        # the streamed event advanced the cursor: collect cannot now qualify
        self.ctl.disarm()
        with self.assertRaises(C.TraceIncomplete):
            self.ctl.collect_campaign(self.profile)

    def test_trace_drain_is_chunked(self):
        ctl = C.PicoController(self.fake, clock=self.clock, drain_chunk=2)
        self.addCleanup(ctl.close)
        ctl.load_image(self.profile)
        ctl.arm(1000)
        for _ in range(5):
            self.fake.target.read(1)
        ctl.disarm()
        events = ctl.drain()
        self.assertEqual(len(events), 5)
        drains = [c for c in self.fake.commands if c.startswith("TRACE DRAIN")]
        self.assertGreaterEqual(len(drains), 3)
        self.assertTrue(all(int(c.split("n=")[1]) == 2 for c in drains))


class TestRefusal(Base):
    queue_limit = 2

    def test_overflow_refuses_campaign(self):
        self.load()
        self.ctl.arm(1000)
        for _ in range(4):
            self.fake.target.read(1)
        self.ctl.disarm()
        with self.assertRaises(C.TraceIncomplete) as ctx:
            self.ctl.collect_campaign(self.profile)
        self.assertIn("dropped", str(ctx.exception).lower())

    def test_trace_end_incomplete_refuses(self):
        self.load()
        self.ctl.arm(1000)
        self.fake.target.read(1)
        with self.assertRaises(C.ControllerError):
            self.ctl.collect_campaign(self.profile)  # still armed -> sealed missing
        self.ctl.disarm()
        camp = self.ctl.collect_campaign(self.profile)
        self.assertTrue(camp.complete)


class TestFailures(Base):
    def test_garbage_status_is_protocol_error(self):
        self.ctl.status()
        self.fake.fail_mode = "garbage"
        self.fake.reply_count = 0
        with self.assertRaises(C.ProtocolError):
            self.ctl.status()

    def test_timeout_is_reported(self):
        self.fake.fail_mode = "timeout"
        with self.assertRaises(C.ControllerError) as ctx:
            self.ctl.status()
        self.assertIn("timeout", str(ctx.exception).lower())

    def test_disconnect_is_reported(self):
        self.fake.fail_mode = "disconnect"
        with self.assertRaises(C.ControllerError) as ctx:
            self.ctl.status()
        self.assertIn("disconnect", str(ctx.exception).lower())

    def test_unknown_error_code_is_protocol_error(self):
        self.fake._pending = ["ERR SOMETHING_NEW detail=1"]
        self.fake.fail_mode = None
        with self.assertRaises(C.ProtocolError):
            self.ctl.status()


class TestCampaignQualification(Base):
    queue_limit = 256  # firmware ring in CB_TRACE_QUEUE

    def test_zero_observation_is_refused(self):
        self.load()
        self.ctl.arm(100)
        self.ctl.disarm()
        with self.assertRaises(C.TraceIncomplete):
            self.ctl.collect_campaign(self.profile)

    def test_partial_drain_then_collect_is_refused(self):
        self.load()
        self.ctl.arm(100)
        self.fake.target.read(1)
        self.ctl.disarm()
        self.assertEqual(len(self.ctl.drain()), 1)
        with self.assertRaises(C.TraceIncomplete) as ctx:
            self.ctl.collect_campaign(self.profile)
        self.assertIn("partially drained", str(ctx.exception))

    def test_truncated_event_is_refused(self):
        self.load()
        self.ctl.arm(100)
        self.fake.target.read(9)
        self.ctl.disarm()
        with self.assertRaises(C.TraceIncomplete) as ctx:
            self.ctl.collect_campaign(self.profile)
        self.assertIn("truncated", str(ctx.exception))

    def test_aborted_transfer_is_refused(self):
        self.load()
        self.ctl.arm(100)
        self.fake.target.read(1)
        self.fake.target.note_abort()
        self.ctl.disarm()
        with self.assertRaises(C.TraceIncomplete) as ctx:
            self.ctl.collect_campaign(self.profile)
        self.assertIn("aborted", str(ctx.exception).lower())

    def test_duplicate_sequence_is_refused(self):
        self.load()
        self.ctl.arm(100)
        self.fake.target.read(1)
        self.fake.target.read(1)
        self.ctl.disarm()
        self.fake.target._queue[1] = dataclasses.replace(
            self.fake.target._queue[1], seq=0)
        with self.assertRaises(C.TraceIncomplete) as ctx:
            self.ctl.collect_campaign(self.profile)
        self.assertIn("duplicate", str(ctx.exception).lower())

    def test_reload_changes_epoch_and_empties_previous_trace(self):
        self.load()
        first_epoch = self.ctl.status().epoch
        self.ctl.arm(100)
        self.fake.target.read(1)
        self.ctl.disarm()
        self.ctl.drain()
        self.load()
        st = self.ctl.status()
        self.assertNotEqual(st.epoch, first_epoch)
        self.assertTrue(st.sealed)
        with self.assertRaises(C.TraceIncomplete):
            self.ctl.collect_campaign(self.profile)

    def test_load_with_undrained_trace_is_refused(self):
        self.load()
        self.ctl.arm(100)
        self.fake.target.read(1)
        self.ctl.disarm()
        with self.assertRaises(C.ProtocolError) as ctx:
            self.load()
        self.assertIn("TRACE_PENDING", str(ctx.exception))

    def test_collect_carries_epoch_and_cfg(self):
        from coralbench import profile as P
        self.load()
        self.ctl.arm(100)
        self.fake.target.read(1)
        self.ctl.disarm()
        camp = self.ctl.collect_campaign(self.profile)
        self.assertEqual(camp.epoch, self.ctl.status().epoch)
        self.assertEqual(camp.config_crc, P.config_crc(self.profile))
        self.assertTrue(camp.complete)

    def test_lease_expiry_without_host(self):
        self.load()
        self.ctl.arm(100)
        self.clock.advance(0.2)
        self.fake.poll()  # the alarm, not a command
        st = self.ctl.status()
        self.assertFalse(st.armed)
        self.assertTrue(st.sealed)
        self.assertTrue(st.expired)

    def test_timeout_poisons_transport(self):
        self.ctl.status()
        self.fake.fail_mode = "timeout"
        with self.assertRaises(C.ControllerError) as ctx:
            self.ctl.status()
        self.assertIn("timeout", str(ctx.exception).lower())
        self.fake.fail_mode = None
        sent = len(self.fake.commands)
        with self.assertRaises(C.ControllerError) as ctx:
            self.ctl.status()
        self.assertIn("poisoned", str(ctx.exception).lower())
        self.assertEqual(len(self.fake.commands), sent)

    def test_streaming_100_events_while_armed_does_not_overflow(self):
        self.load()
        self.ctl.arm(2000)
        for _ in range(100):
            self.fake.target.read(1)
        events = self.ctl.drain()
        self.assertEqual(len(events), 100)
        self.assertEqual([e.seq for e in events], list(range(100)))
        st = self.ctl.status()
        self.assertTrue(st.armed)
        self.assertEqual(st.trace_dropped, 0)
        self.assertEqual(st.trace_drained, 100)


if __name__ == "__main__":
    unittest.main()
