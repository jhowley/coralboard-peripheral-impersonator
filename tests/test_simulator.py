import dataclasses
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from coralbench import profile as P  # noqa: E402
from coralbench import simulator as S  # noqa: E402

GOOD = os.path.join(ROOT, "profiles", "bench-temp-raw-v1.json")
ALT = os.path.join(ROOT, "profiles", "bench-temp-alt-v1.json")


class Base(unittest.TestCase):
    def setUp(self):
        self.profile = P.load_profile(GOOD)
        self.target = S.SimulatedTarget(self.profile)
        self.target.arm()

    def tearDown(self):
        if self.target.armed:
            self.target.disarm()


class TestPointerAndImage(Base):
    def test_default_read(self):
        self.assertEqual(self.target.read(1), b"\x19")
        self.assertEqual(self.target.read(3), b"\x60\x4b\x50")

    def test_pointer_write_then_read(self):
        self.assertEqual(self.target.write(b"\x01"), 0)
        self.assertEqual(self.target.read(1), b"\x60")

    def test_pointer_plus_data_write_lands_in_image(self):
        accepted = self.target.write(b"\x01\x77\x88")
        self.assertEqual(accepted, 2)
        self.assertEqual(self.target.write(b"\x02"), 0)
        # wrap=4: data bytes land at 1 then 2; pointer ends at 3.
        self.assertEqual(self.target.read(2), b"\x88\x50")

    def test_write_outside_window_is_rejected_and_counted(self):
        accepted = self.target.write(b"\x10\xaa\xbb")
        # D8: pointer 0x10 is reduced by the declared modulus first (0x10 % 4),
        # so 0xaa hits read-only reg 0 and 0xbb hits writable reg 1.
        self.assertEqual(accepted, 1)
        self.assertEqual(self.target.rejected_writes, 1)
        self.target.write(b"\x00")
        self.assertEqual(self.target.read(1), b"\x19")

    def test_pointer_write_honours_wrap_modulus(self):
        self.assertEqual(self.target.write(b"\x10"), 0)
        self.assertEqual(self.target.pointer, 0)
        self.assertEqual(self.target.read(1), b"\x19")

    def test_initial_pointer_is_applied(self):
        alt = P.load_profile(ALT)
        initial = dataclasses.replace(alt, pointer_initial=2)
        t = S.SimulatedTarget(initial)
        t.arm()
        self.assertEqual(t.pointer, 2)
        self.assertEqual(t.read(1), b"\x40")

    def test_auto_increment_and_wrap(self):
        self.target.write(b"\x00")
        self.assertEqual(self.target.read(2), b"\x19\x60")
        self.assertEqual(self.target.pointer, 2)
        # wrap=4: pointer 2 + 4 read bytes = 2,3,0,1 -> 4b 50 19 60
        self.assertEqual(self.target.read(4), b"\x4b\x50\x19\x60")
        self.assertEqual(self.target.pointer, 2)  # (2+4) % 4

    def test_no_auto_increment_profile(self):
        alt = P.load_profile(ALT)
        t = S.SimulatedTarget(alt)
        t.arm()
        t.write(b"\x02")
        self.assertEqual(t.read(1), b"\x40")
        self.assertEqual(t.read(1), b"\x40")
        self.assertEqual(t.pointer, 2)

    def test_empty_write_rejected(self):
        with self.assertRaises(ValueError):
            self.target.write(b"")

    def test_bad_read_length(self):
        with self.assertRaises(ValueError):
            self.target.read(0)
        with self.assertRaises(ValueError):
            self.target.read(257)


class TestTrace(Base):
    def test_trace_is_byte_level_and_ordered(self):
        self.target.write(b"\x01")
        self.target.read(2)
        events = self.target.drain()
        self.assertEqual([e.op for e in events], ["w", "r"])
        self.assertEqual([e.seq for e in events], [0, 1])
        self.assertEqual(events[0].ptr, 1)
        self.assertEqual(events[1].data, b"\x60\x4b")
        self.assertFalse(any(e.truncated for e in events))

    def test_long_read_is_truncated_in_trace_but_not_on_the_wire(self):
        data = self.target.read(16)
        self.assertEqual(len(data), 16)
        event = self.target.drain()[0]
        self.assertEqual(event.length, 16)
        self.assertEqual(len(event.data), S.TRACE_DATA_MAX)
        self.assertTrue(event.truncated)

    def test_256_byte_read_keeps_full_length(self):
        data = self.target.read(256)
        self.assertEqual(len(data), 256)
        event = self.target.drain()[0]
        self.assertEqual(event.length, 256)  # not clamped to 255
        self.assertTrue(event.truncated)

    def test_repeated_start_read_is_two_segments(self):
        self.target.write(b"\x01")
        self.target.read(2)
        events = self.target.drain()
        self.assertEqual([(e.op, e.ptr, e.length) for e in events],
                         [("w", 1, 0), ("r", 1, 2)])
        self.assertEqual({e.epoch for e in events}, {self.target.epoch})

    def test_drain_empties_queue(self):
        self.target.read(1)
        self.assertEqual(len(self.target.drain()), 1)
        self.assertEqual(self.target.drain(), [])

    def test_sealed_after_disarm_keeps_events(self):
        self.target.read(1)
        self.target.disarm()
        self.assertTrue(self.target.sealed)
        self.assertEqual(len(self.target.drain()), 1)
        with self.assertRaises(S.NotArmed):
            self.target.read(1)

    def test_arm_is_required(self):
        self.target.disarm()
        with self.assertRaises(S.NotArmed):
            self.target.write(b"\x00")

    def test_double_arm_rejected(self):
        with self.assertRaises(S.SimulatorError):
            self.target.arm()


class TestOverflow(Base):
    def test_overflow_counts_drops_and_blocks_completeness(self):
        t = S.SimulatedTarget(self.profile, queue_limit=2)
        t.arm()
        for _ in range(3):
            t.read(1)
        self.assertTrue(t.overflowed)
        self.assertEqual(t.dropped, 1)
        self.assertEqual(len(t.drain()), 2)
        with self.assertRaises(S.TraceOverflow):
            t.assert_complete()

    def test_clean_run_asserts_complete(self):
        self.target.read(2)
        self.assertEqual(len(self.target.drain()), 1)
        self.target.disarm()
        self.target.assert_complete()


class TestEpochAndLifecycle(Base):
    def test_epoch_changes_on_arm_and_reload(self):
        first = self.target.epoch
        self.target.disarm()
        self.target.arm()
        second = self.target.epoch
        self.assertNotEqual(first, second)
        self.assertEqual([e.epoch for e in self.target.drain()], [])
        self.target.disarm()
        self.target.reload(self.profile)
        self.assertNotEqual(self.target.epoch, second)

    def test_events_carry_epoch_and_sequence_from_zero(self):
        self.target.read(1)
        self.target.read(1)
        events = self.target.drain()
        self.assertEqual([e.seq for e in events], [0, 1])
        self.assertEqual({e.epoch for e in events}, {self.target.epoch})

    def test_arm_restores_template(self):
        self.target.write(b"\x01\x99")
        self.target.disarm()
        self.target.arm()
        self.assertEqual(self.target.pointer, 0)
        self.assertEqual(self.target.read(2), b"\x19\x60")

    def test_drain_while_armed_is_streaming_not_complete(self):
        self.target.read(1)
        events = self.target.drain()
        self.assertEqual(len(events), 1)
        self.assertTrue(self.target.armed)
        self.assertFalse(self.target.sealed)
        self.assertFalse(self.target.complete_candidate)

    def test_abort_counts_and_blocks_completeness(self):
        self.target.note_abort()
        self.target.disarm()
        self.assertFalse(self.target.complete_candidate)
        with self.assertRaises(S.SimulatorError):
            self.target.assert_complete()

    def test_zero_observation_is_not_complete(self):
        self.target.disarm()
        self.assertFalse(self.target.complete_candidate)
        with self.assertRaises(S.SimulatorError):
            self.target.assert_complete()

    def test_reload_refuses_undrained_events(self):
        self.target.read(1)
        self.target.disarm()
        with self.assertRaises(S.SimulatorError):
            self.target.reload(self.profile)
        self.target.drain()
        self.target.reload(self.profile)


class TestDeterminism(Base):
    def test_same_ops_same_bytes_and_trace(self):
        def run():
            t = S.SimulatedTarget(P.load_profile(GOOD))
            t.arm()
            t.write(b"\x01\x77")
            t.read(3)
            t.write(b"\x02\x11\x22")
            t.read(4)
            t.disarm()
            run_events = [e.as_dict() for e in t.drain()]
            return run_events, t.digest()

        first, second = run(), run()
        self.assertEqual(first, second)
        self.assertEqual(len(first[1]), 64)


if __name__ == "__main__":
    unittest.main()
