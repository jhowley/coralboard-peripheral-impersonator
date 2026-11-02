"""Behavioural tests for the userspace USB CDC adapter.

Written before ``coralbench.usb_link`` existed; the initial run failed with an
import error for that module. Nothing here touches hardware or a real libusb
handle: the backend fake mirrors libusb's return-count/timeout semantics,
including timeout *with* partial bytes.
"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from coralbench import cli                       # noqa: E402
from coralbench import profile as prof           # noqa: E402
from coralbench import usb_link as U             # noqa: E402
from support import fake_libusb as F             # noqa: E402
from support.fake_pico import FakePicoTransport  # noqa: E402

GOOD = os.path.join(ROOT, "profiles", "bench-temp-raw-v1.json")
SERIAL = "E660000000000000"


def make_protocol():
    return FakePicoTransport(prof.load_profile(GOOD))


def opened(device):
    fake = F.FakeLibUSB([device])
    link = U.USBLink(SERIAL, timeout_s=0.5, backend=fake)
    link.open()
    return fake, device, link


def make_observed_config(*, comm_protocol=0x00):
    """Field-for-field shape reported by ``lsusb -v`` on the board.

    Interface 0: CDC control 02/02/00, interrupt IN 0x81, 8-byte packets.
    Interface 1: CDC data 0a/00/00, bulk OUT 0x02 / IN 0x82, 64-byte packets.
    Interface 2: vendor/reset ff/00/00, present but never claimed.
    """
    return U.ConfigurationDescriptor(value=1, interfaces=(
        U.InterfaceDescriptor(number=0, alt_setting=0, class_code=0x02,
                              subclass_code=0x02, protocol_code=comm_protocol,
                              endpoints=(F.make_endpoint(0x81, 0x03, 8),)),
        U.InterfaceDescriptor(number=1, alt_setting=0, class_code=0x0A,
                              subclass_code=0x00, protocol_code=0x00,
                              endpoints=(F.make_endpoint(0x02, 0x02, 64),
                                         F.make_endpoint(0x82, 0x02, 64))),
        U.InterfaceDescriptor(number=2, alt_setting=0, class_code=0xFF,
                              subclass_code=0x00, protocol_code=0x00,
                              endpoints=(F.make_endpoint(0x83, 0x02, 64),)),
    ))


class TestSerialNumberValidation(unittest.TestCase):
    def test_accepts_the_observed_serial(self):
        self.assertEqual(U.check_serial_number(SERIAL), SERIAL)

    def test_rejects_empty_and_non_string(self):
        for value in ("", "   ", None, 123, b"E660000000000000"):
            with self.subTest(value=value):
                with self.assertRaises(U.USBLinkError):
                    U.check_serial_number(value)

    def test_rejects_globs_and_paths(self):
        for value in ("E6600*", "E6600?", "E6600[0]", "/dev/bus/usb/001/002",
                      "E660000000000000\n", "E66000 0000000000"):
            with self.subTest(value=value):
                with self.assertRaises(U.USBLinkError):
                    U.check_serial_number(value)


class TestDeviceSelection(unittest.TestCase):
    def test_selects_only_the_requested_serial(self):
        other = F.make_device(serial="0000000000000001")
        wanted = F.make_device(serial=SERIAL)
        fake = F.FakeLibUSB([other, wanted])
        link = U.USBLink(SERIAL, timeout_s=0.5, backend=fake)
        link.open()
        try:
            self.assertEqual(wanted.claims, [U.COMM_INTERFACE, U.DATA_INTERFACE])
            self.assertEqual(other.claims, [])
            self.assertTrue(all(h.closed for h in other.handles))
            self.assertFalse(any(h.closed for h in wanted.handles))
        finally:
            link.close()

    def test_duplicate_serial_is_ambiguous_and_refused(self):
        a = F.make_device(serial=SERIAL)
        b = F.make_device(serial=SERIAL)
        fake = F.FakeLibUSB([a, b])
        link = U.USBLink(SERIAL, timeout_s=0.5, backend=fake)
        with self.assertRaises(U.USBLinkError) as ctx:
            link.open()
        self.assertIn("ambiguous", str(ctx.exception))
        self.assertEqual(a.claims, [])
        self.assertEqual(b.claims, [])
        self.assertTrue(all(h.closed for h in a.handles + b.handles))
        self.assertEqual(fake.exit_calls, 1)

    def test_missing_serial_reports_what_was_seen(self):
        fake = F.FakeLibUSB([F.make_device(serial="0000000000000001")])
        link = U.USBLink(SERIAL, timeout_s=0.5, backend=fake)
        with self.assertRaises(U.USBLinkError) as ctx:
            link.open()
        message = str(ctx.exception)
        self.assertIn(SERIAL, message)
        self.assertIn("0000000000000001", message)
        self.assertEqual(fake.exit_calls, 1)

    def test_no_matching_vendor_product_is_refused(self):
        fake = F.FakeLibUSB([F.make_device(serial=SERIAL, vendor_id=0x1234)])
        link = U.USBLink(SERIAL, timeout_s=0.5, backend=fake)
        with self.assertRaises(U.USBLinkError) as ctx:
            link.open()
        self.assertIn("2e8a:000a", str(ctx.exception))
        self.assertEqual(fake.devices[0].claims, [])

    def test_never_claims_the_vendor_reset_interface(self):
        fake, device, link = opened(F.make_device(serial=SERIAL))
        try:
            self.assertEqual(device.claims, [0, 1])
            self.assertNotIn(2, device.claims)
            self.assertEqual(device.released, [])
        finally:
            link.close()
        self.assertEqual(device.released, [1, 0])


class TestOpenFailures(unittest.TestCase):
    def test_bad_descriptor_fails_closed_before_claim(self):
        variants = {
            "no interrupt endpoint": dict(interrupt=False),
            "no bulk in": dict(bulk_in=False),
            "no bulk out": dict(bulk_out=False),
            "wrong bulk packet size": dict(max_packet_size=32),
            "wrong comm class": dict(comm_class=0x0A),
            "wrong comm protocol": dict(comm_protocol=0x01),
            "wrong data class": dict(data_class=0x02),
            "wrong interrupt endpoint": dict(comm_endpoint=0x84),
        }
        for label, kwargs in variants.items():
            with self.subTest(label):
                device = F.make_device(serial=SERIAL,
                                       config=F.make_config(**kwargs))
                fake = F.FakeLibUSB([device])
                link = U.USBLink(SERIAL, timeout_s=0.5, backend=fake)
                with self.assertRaises(U.USBLinkError):
                    link.open()
                self.assertEqual(device.claims, [])
                self.assertEqual(device.control_calls, [])
                self.assertEqual(fake.exit_calls, 1)

    def test_missing_data_interface_fails_closed(self):
        device = F.make_device(serial=SERIAL)
        device.config = U.ConfigurationDescriptor(value=1, interfaces=(
            device.config.interfaces[0],))
        fake = F.FakeLibUSB([device])
        link = U.USBLink(SERIAL, timeout_s=0.5, backend=fake)
        with self.assertRaises(U.USBLinkError):
            link.open()
        self.assertEqual(device.claims, [])

    def test_active_kernel_driver_is_refused_not_detached(self):
        device = F.make_device(serial=SERIAL,
                               kernel_driver_interfaces={0: 1})
        fake = F.FakeLibUSB([device])
        link = U.USBLink(SERIAL, timeout_s=0.5, backend=fake)
        with self.assertRaises(U.USBLinkError) as ctx:
            link.open()
        self.assertIn("kernel driver", str(ctx.exception).lower())
        self.assertEqual(device.claims, [])
        self.assertEqual(device.control_calls, [])
        self.assertEqual(fake.set_auto_detach_calls, 0)
        self.assertEqual(fake.detach_calls, 0)
        self.assertEqual(fake.reset_calls, 0)
        self.assertEqual(fake.exit_calls, 1)

    def test_claim_failure_releases_the_first_interface_and_closes(self):
        device = F.make_device(serial=SERIAL)
        device.claim_failed = {1}
        fake = F.FakeLibUSB([device])
        link = U.USBLink(SERIAL, timeout_s=0.5, backend=fake)
        with self.assertRaises(U.USBLinkError):
            link.open()
        self.assertEqual(device.claims, [0])
        self.assertEqual(device.released, [0])
        self.assertTrue(all(h.closed for h in device.handles))
        self.assertEqual(fake.exit_calls, 1)
        self.assertFalse(fake.lists[0].disposed is False)

    def test_cdc_setup_failure_releases_both_interfaces(self):
        device = F.make_device(serial=SERIAL)
        device.control_reply = -1
        fake = F.FakeLibUSB([device])
        link = U.USBLink(SERIAL, timeout_s=0.5, backend=fake)
        with self.assertRaises(U.USBLinkError):
            link.open()
        self.assertEqual(sorted(device.released), [0, 1])
        self.assertTrue(all(h.closed for h in device.handles))
        self.assertEqual(fake.exit_calls, 1)

    def test_cdc_setup_values_and_no_reset(self):
        device = F.make_device(serial=SERIAL)
        fake, device, link = opened(device)
        try:
            self.assertEqual(len(device.control_calls), 2)
            coding, state = device.control_calls
            self.assertEqual((coding["request_type"], coding["request"]), (0x21, 0x20))
            self.assertEqual(coding["value"], 0)
            self.assertEqual(coding["index"], 0)
            self.assertEqual(coding["data"], bytes([0x00, 0xC2, 0x01, 0x00, 0x00, 0x00, 0x08]))
            self.assertEqual((state["request_type"], state["request"]), (0x21, 0x22))
            self.assertEqual(state["value"], 3)
            self.assertEqual(state["data"], None)
            self.assertEqual(fake.reset_calls, 0)
        finally:
            link.close()

    def test_close_is_idempotent(self):
        fake, device, link = opened(F.make_device(serial=SERIAL))
        link.close()
        link.close()
        self.assertEqual(device.released, [1, 0])


class TestObservedDescriptor(unittest.TestCase):
    """Regression for the live blocker: control protocol is 0x00, not 0x01.

    The earlier test double encoded 0x01, so 185 tests passed against a
    descriptor the board does not report. These tests pin the actual shape.
    """

    def test_verify_config_accepts_the_observed_descriptor(self):
        self.assertEqual(U.verify_config(make_observed_config()), (0x82, 0x02))

    def test_observed_descriptor_opens_without_claiming_interface_2(self):
        _fake, device, link = opened(F.make_device(serial=SERIAL,
                                                   config=make_observed_config()))
        try:
            self.assertEqual(device.claims, [0, 1])
            self.assertEqual(device.released, [])
        finally:
            link.close()
        self.assertEqual(device.released, [1, 0])

    def test_control_protocol_0x01_is_refused(self):
        device = F.make_device(serial=SERIAL,
                               config=make_observed_config(comm_protocol=0x01))
        fake = F.FakeLibUSB([device])
        link = U.USBLink(SERIAL, timeout_s=0.5, backend=fake)
        with self.assertRaises(U.USBLinkError) as ctx:
            link.open()
        self.assertIn("protocol=0x01", str(ctx.exception))
        self.assertEqual(device.claims, [])
        self.assertEqual(fake.exit_calls, 1)

    def test_fake_default_config_is_the_observed_descriptor(self):
        config = F.make_config()
        comm, data, reset = config.interfaces
        self.assertEqual((comm.class_code, comm.subclass_code, comm.protocol_code),
                         (0x02, 0x02, 0x00))
        self.assertEqual([(e.address, e.transfer_type, e.max_packet_size)
                          for e in comm.endpoints], [(0x81, 0x03, 8)])
        self.assertEqual((data.class_code, data.subclass_code, data.protocol_code),
                         (0x0A, 0x00, 0x00))
        self.assertEqual([(e.address, e.transfer_type, e.max_packet_size)
                          for e in data.endpoints],
                         [(0x02, 0x02, 64), (0x82, 0x02, 64)])
        self.assertEqual(reset.number, 2)


class TestReadSemantics(unittest.TestCase):
    def setUp(self):
        self.fake, self.device, self.link = opened(F.make_device(serial=SERIAL))

    def tearDown(self):
        self.link.close()

    def test_fragmented_line_is_reassembled(self):
        self.device.read_script = [(0, b"OK STAT"), (0, b"US armed=0\n")]
        self.assertEqual(self.link.read_line(0.5), "OK STATUS armed=0")

    def test_coalesced_lines_are_split_without_extra_transfers(self):
        self.device.read_script = [(0, b"OK ONE\nOK TWO\n")]
        self.assertEqual(self.link.read_line(0.5), "OK ONE")
        before = len(self.device.bulk_calls)
        self.assertEqual(self.link.read_line(0.5), "OK TWO")
        self.assertEqual(len(self.device.bulk_calls), before)

    def test_timeout_with_partial_bytes_keeps_the_buffer(self):
        self.device.read_script = [(-7, b"OK PART")]
        self.assertIsNone(self.link.read_line(0.05))
        self.device.read_script = [(0, b"IAL\n")]
        self.assertEqual(self.link.read_line(0.5), "OK PARTIAL")

    def test_timeout_returns_none_with_bounded_ms(self):
        self.assertIsNone(self.link.read_line(0.02))
        self.assertTrue(self.device.bulk_calls)
        for call in self.device.bulk_calls:
            self.assertGreaterEqual(call["timeout_ms"], 1)
            self.assertLessEqual(call["timeout_ms"], 5000)

    def test_overlong_incoming_line_is_an_error(self):
        self.device.read_script = [(0, b"X" * (U.MAX_LINE + 1) + b"\n")]
        with self.assertRaises(U.USBLinkError):
            self.link.read_line(0.5)

    def test_unterminated_buffer_budget_is_an_error(self):
        self.device.read_script = [(0, b"X" * U.READ_CHUNK),
                                   (0, b"Y" * 16)]
        with self.assertRaises(U.USBLinkError) as ctx:
            self.link.read_line(0.5)
        self.assertIn("newline", str(ctx.exception))

    def test_non_ascii_reply_is_an_error(self):
        self.device.read_script = [(0, b"\xff\xfe\n")]
        with self.assertRaises(U.USBLinkError):
            self.link.read_line(0.5)

    def test_device_gone_poisons_the_link(self):
        self.device.read_script = [(-4, b"")]
        with self.assertRaises(U.USBLinkError):
            self.link.read_line(0.5)
        before = len(self.device.bulk_calls)
        with self.assertRaises(U.USBLinkError):
            self.link.write_line("STATUS")
        self.assertEqual(len(self.device.bulk_calls), before)


class TestWriteSemantics(unittest.TestCase):
    def setUp(self):
        self.fake, self.device, self.link = opened(F.make_device(serial=SERIAL))

    def tearDown(self):
        self.link.close()

    def test_partial_write_is_resumed(self):
        self.device.write_script = [(-7, 3), (0, 4)]
        self.link.write_line("STATUS")
        self.assertEqual(bytes(self.device.received), b"STATUS\n")

    def test_no_progress_write_times_out(self):
        self.device.write_script = [(-7, 0)] * 1000
        with self.assertRaises(U.USBLinkError):
            self.link.write_line("STATUS", timeout_s=0.02)
        self.assertEqual(bytes(self.device.received), b"")

    def test_rejects_newline_and_overlong_and_non_ascii_before_transfer(self):
        for value in ("STATUS\nARM", "X" * (U.MAX_LINE + 1), "\u00e9"):
            with self.subTest(value=value[:12]):
                with self.assertRaises(U.USBLinkError):
                    self.link.write_line(value)
        self.assertEqual(self.device.bulk_calls, [])

    def test_send_line_is_an_alias(self):
        self.link.send_line("STATUS")
        self.assertEqual(bytes(self.device.received), b"STATUS\n")


class TestRealLibUSBSurface(unittest.TestCase):
    """ABI/loading checks only; no handle is opened and no device is touched."""

    def test_load_missing_library_is_a_usb_error(self):
        with self.assertRaises(U.USBLinkError):
            U.LibUSBBackend.load("/nonexistent/libusb-1.0.so.0")

    def test_descriptor_struct_offsets_match_the_c_abi(self):
        dev = U.LibusbDeviceDescriptor
        self.assertEqual(U.ctypes.sizeof(dev), 18)
        self.assertEqual(dev.bcdUSB.offset, 2)
        self.assertEqual(dev.idVendor.offset, 8)
        self.assertEqual(dev.idProduct.offset, 10)
        self.assertEqual(dev.iSerialNumber.offset, 16)
        self.assertEqual(dev.bNumConfigurations.offset, 17)

        ep = U.LibusbEndpointDescriptor
        self.assertEqual(ep.bEndpointAddress.offset, 2)
        self.assertEqual(ep.bmAttributes.offset, 3)
        self.assertEqual(ep.wMaxPacketSize.offset, 4)

        cfg = U.LibusbConfigDescriptor
        self.assertEqual(cfg.wTotalLength.offset, 2)
        self.assertEqual(cfg.bNumInterfaces.offset, 4)
        self.assertEqual(cfg.bConfigurationValue.offset, 5)
        self.assertEqual(cfg.bmAttributes.offset, 7)
        self.assertEqual(cfg.MaxPower.offset, 8)

    def test_timeout_conversion_never_passes_zero(self):
        for value in (0, -1, 0.0001):
            self.assertEqual(U.timeout_to_ms(value), 1)
        self.assertEqual(U.timeout_to_ms(2.0), 2000)

    def test_error_name_mapping(self):
        self.assertEqual(U.libusb_error_name(-7), "LIBUSB_ERROR_TIMEOUT")
        self.assertEqual(U.libusb_error_name(0), "LIBUSB_SUCCESS")


class TestUsbCli(unittest.TestCase):
    def run_cli(self, fake, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(U, "load_backend", lambda: fake):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main(["usb", "--serial", SERIAL, *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_status_through_the_real_cli(self):
        fake = F.FakeLibUSB([F.make_device(serial=SERIAL,
                                          protocol=make_protocol())])
        code, out, err = self.run_cli(fake, "status")
        self.assertEqual(code, 0, err)
        self.assertIn("loaded=0", out)
        self.assertIn("addr=0x48", out)
        self.assertEqual(fake.devices[0].protocol.commands, ["STATUS"])

    def test_load_arm_disarm_sequence(self):
        fake = F.FakeLibUSB([F.make_device(serial=SERIAL,
                                          protocol=make_protocol())])
        code, out, err = self.run_cli(fake, "load", "--profile", GOOD)
        self.assertEqual(code, 0, err)
        self.assertIn("loaded profile=bench-temp-raw-v1", out)
        code, out, err = self.run_cli(fake, "arm", "--lease-ms", "250")
        self.assertEqual(code, 0, err)
        self.assertIn("armed=1", out)
        code, out, err = self.run_cli(fake, "disarm")
        self.assertEqual(code, 0, err)
        self.assertIn("armed=0", out)

    def test_collect_writes_complete_campaign(self):
        protocol = make_protocol()
        fake = F.FakeLibUSB([F.make_device(serial=SERIAL, protocol=protocol)])
        self.assertEqual(self.run_cli(fake, "load", "--profile", GOOD)[0], 0)
        self.assertEqual(self.run_cli(fake, "arm", "--lease-ms", "1000")[0], 0)
        protocol.target.read(2)
        self.assertEqual(self.run_cli(fake, "disarm")[0], 0)
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "campaign.jsonl")
            code, text, err = self.run_cli(fake, "collect", "--profile", GOOD,
                                           "--out", out)
            self.assertEqual(code, 0, err)
            self.assertIn("campaign complete", text)
            import json
            records = [json.loads(line) for line in open(out) if line.strip()]
            self.assertTrue(records[-1]["complete"])
            trace = [r for r in records if r.get("type") == "trace"]
            self.assertEqual([r["seq"] for r in trace], [0])

    def test_lease_bound_is_checked_before_opening_the_device(self):
        fake = F.FakeLibUSB([F.make_device(serial=SERIAL,
                                          protocol=make_protocol())])
        code, out, err = self.run_cli(fake, "arm", "--lease-ms", "12001")
        self.assertEqual(code, 1)
        self.assertIn("lease", err.lower())
        self.assertEqual(fake.init_calls, 0)

    def test_ambiguous_device_is_refused_and_exits_nonzero(self):
        fake = F.FakeLibUSB([F.make_device(serial=SERIAL),
                             F.make_device(serial=SERIAL)])
        code, out, err = self.run_cli(fake, "status")
        self.assertEqual(code, 1)
        self.assertIn("ambiguous", err)
        self.assertIn("usb:", err)

    def test_no_device_is_refused(self):
        fake = F.FakeLibUSB([])
        code, out, err = self.run_cli(fake, "status")
        self.assertEqual(code, 1)
        self.assertIn("2e8a:000a", err)

    def test_serial_number_argument_is_required(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["usb", "status"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
