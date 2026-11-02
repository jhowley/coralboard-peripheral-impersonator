"""PosixSerial against a real pty pair: no hardware, but real termios/defaults."""

import os
import pty
import select
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from coralbench import serial_link as L  # noqa: E402


class TestDevicePath(unittest.TestCase):
    def test_missing_path_is_rejected(self):
        with self.assertRaises(L.SerialLinkError):
            L.check_device_path("/dev/definitely-not-a-tty-xyz")

    def test_glob_is_rejected(self):
        with self.assertRaises(L.SerialLinkError) as ctx:
            L.check_device_path("/dev/ttyACM*")
        self.assertIn("glob", str(ctx.exception))

    def test_relative_path_is_rejected(self):
        with self.assertRaises(L.SerialLinkError):
            L.check_device_path("ttyACM0")

    def test_directory_is_rejected(self):
        with self.assertRaises(L.SerialLinkError):
            L.check_device_path("/dev")


class TestPty(unittest.TestCase):
    def setUp(self):
        self.master, self.slave = pty.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)
        self.path = os.ttyname(self.slave)
        self.link = L.PosixSerial(self.path, timeout_s=1.0)
        self.link.open()
        self.addCleanup(self.link.close)

    def test_device_to_host_line(self):
        os.write(self.master, b"OK STATUS armed=0\n")
        self.assertEqual(self.link.read_line(1.0), "OK STATUS armed=0")

    def test_partial_line_is_buffered(self):
        os.write(self.master, b"OK DRA")
        self.assertIsNone(self.link.read_line(0.1))
        os.write(self.master, b"IN n=2\n")
        self.assertEqual(self.link.read_line(1.0), "OK DRAIN n=2")

    def test_host_to_device_write(self):
        self.link.write_line("STATUS")
        ready, _, _ = select.select([self.master], [], [], 1.0)
        self.assertTrue(ready)
        self.assertEqual(os.read(self.master, 64), b"STATUS\n")

    def test_timeout_returns_none(self):
        self.assertIsNone(self.link.read_line(0.05))

    def test_rejects_newline_in_command(self):
        with self.assertRaises(L.SerialLinkError):
            self.link.write_line("STATUS\nARM lease_ms=100")

    def test_rejects_overlong_command(self):
        with self.assertRaises(L.SerialLinkError):
            self.link.write_line("X" * (L.MAX_LINE + 1))

    def test_overlong_incoming_line_is_an_error(self):
        os.write(self.master, b"Y" * (L.MAX_LINE + 8) + b"\n")
        with self.assertRaises(L.SerialLinkError):
            self.link.read_line(1.0)

    def test_close_is_idempotent(self):
        self.link.close()
        self.link.close()


if __name__ == "__main__":
    unittest.main()
