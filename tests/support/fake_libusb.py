"""A libusb API-shaped test double. NO HARDWARE, NO REAL LIBUSB.

The fake mirrors the *semantics* the adapter relies on rather than mocking the
adapter's own methods:

* ``bulk_transfer`` takes a buffer/length/timeout in milliseconds and returns
  ``(rc, transferred, bytes)``; a transfer may return ``LIBUSB_ERROR_TIMEOUT``
  (-7) *with a non-zero transferred count*, which real libusb does.
* handles and contexts have real open/close/claim state, so cleanup can be
  asserted.
* descriptors are the same frozen dataclasses the adapter verifies, built from
  the observed Pico layout unless a test asks for a malformed one.

An optional ``protocol`` device (tests/support/fake_pico.py) turns complete
OUT lines into replies, so CLI tests exercise the real controller and CLI code
over a labelled fake device.
"""

from __future__ import annotations

from coralbench import usb_link as U

LIBUSB_ERROR_TIMEOUT = -7


class FakeDevice:
    def __init__(self, *, serial="E660000000000000", vendor_id=U.DEVICE_VID,
                 product_id=U.DEVICE_PID, config=None, protocol=None,
                 serial_index=3, kernel_driver_interfaces=None):
        self.serial = serial
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.serial_index = serial_index
        self.config = config if config is not None else make_config()
        self.protocol = protocol
        self.kernel_driver_interfaces = dict(kernel_driver_interfaces or {})
        self.handles = []
        self.claims = []
        self.released = []
        self.control_calls = []
        self.bulk_calls = []
        self.received = bytearray()
        self._rx = b""
        self.inbox = []            # [(rc, bytes)] for IN transfers
        self.read_script = []      # overrides inbox when non-empty
        self.write_script = []     # [(rc, n)] for OUT transfers
        self.claim_failed = set()
        self.control_reply = None  # override control transfer return


class FakeHandle:
    def __init__(self, device):
        self.device = device
        self.claimed = []
        self.closed = False


class FakeContext:
    def __init__(self):
        self.open = True


class FakeDeviceList:
    def __init__(self, devices):
        self._devices = tuple(devices)
        self.disposed = False

    def __iter__(self):
        return iter(self._devices)

    def __len__(self):
        return len(self._devices)

    def dispose(self):
        self.disposed = True


class FakeLibUSB:
    """Implements the backend surface used by coralbench.usb_link.USBLink."""

    def __init__(self, devices=()):
        self.devices = list(devices)
        self.init_calls = 0
        self.exit_calls = 0
        self.handle_close_calls = 0
        self.set_auto_detach_calls = 0
        self.detach_calls = 0
        self.reset_calls = 0
        self.contexts = []
        self.lists = []

    # -- context / enumeration -----------------------------------------
    def init(self):
        self.init_calls += 1
        ctx = FakeContext()
        self.contexts.append(ctx)
        return ctx

    def exit(self, ctx):
        self.exit_calls += 1
        ctx.open = False

    def list_devices(self, ctx):
        assert ctx.open
        listing = FakeDeviceList(self.devices)
        self.lists.append(listing)
        return listing

    def device_descriptor(self, dev):
        return U.DeviceDescriptor(vendor_id=dev.vendor_id,
                                  product_id=dev.product_id,
                                  serial_index=dev.serial_index)

    def open(self, ctx, dev):
        assert ctx.open
        handle = FakeHandle(dev)
        dev.handles.append(handle)
        return handle

    def close(self, handle):
        self.handle_close_calls += 1
        handle.closed = True

    def string_descriptor(self, handle, index):
        dev = handle.device
        if index != dev.serial_index or dev.serial is None:
            raise U.USBLinkError("fake: no serial descriptor")
        return dev.serial

    # -- configuration --------------------------------------------------
    def config_descriptor(self, dev):
        return dev.config

    def kernel_driver_active(self, handle, interface):
        return handle.device.kernel_driver_interfaces.get(interface, 0)

    def claim_interface(self, handle, interface):
        if interface in handle.device.claim_failed:
            raise U.USBLinkError(f"fake: cannot claim interface {interface}")
        handle.claimed.append(interface)
        handle.device.claims.append(interface)

    def release_interface(self, handle, interface):
        handle.device.released.append(interface)
        if interface in handle.claimed:
            handle.claimed.remove(interface)

    # -- transfers ------------------------------------------------------
    def control_transfer(self, handle, request_type, request, value, index,
                         data, timeout_ms):
        dev = handle.device
        dev.control_calls.append({
            "request_type": request_type, "request": request, "value": value,
            "index": index, "data": data, "timeout_ms": timeout_ms,
        })
        if dev.control_reply is not None:
            return dev.control_reply
        return len(data) if data is not None else 0

    def bulk_transfer(self, handle, endpoint, data, length, timeout_ms):
        dev = handle.device
        dev.bulk_calls.append({"endpoint": endpoint, "length": length,
                               "timeout_ms": timeout_ms,
                               "out_len": None if data is None else len(data)})
        if handle.closed:
            return -4, 0, b""  # LIBUSB_ERROR_NO_DEVICE
        if endpoint == U.BULK_OUT_ENDPOINT:
            if data is None:
                return -1, 0, b""
            if dev.write_script:
                rc, count = dev.write_script.pop(0)
            else:
                rc, count = 0, len(data)
            count = max(0, min(count, len(data)))
            if count:
                dev.received += data[:count]
                dev._rx += data[:count]
                self._feed(dev)
            return rc, count, b""
        if endpoint == U.BULK_IN_ENDPOINT:
            if dev.read_script:
                rc, payload = dev.read_script.pop(0)
            elif dev.inbox:
                rc, payload = dev.inbox.pop(0)
            else:
                rc, payload = LIBUSB_ERROR_TIMEOUT, b""
            return rc, len(payload), payload
        return -1, 0, b""

    def _feed(self, dev):
        if dev.protocol is None:
            return
        while b"\n" in dev._rx:
            line, dev._rx = dev._rx.split(b"\n", 1)
            dev.protocol.send_line(line.decode("ascii", "replace"))
            while True:
                reply = dev.protocol.read_line(0)
                if reply is None:
                    break
                dev.inbox.append((0, (reply + "\n").encode("ascii")))

    # -- things the adapter must never call -----------------------------
    def set_auto_detach_kernel_driver(self, *args):
        self.set_auto_detach_calls += 1

    def detach_kernel_driver(self, *args):
        self.detach_calls += 1

    def reset_device(self, *args):
        self.reset_calls += 1


def make_endpoint(address, attributes, max_packet_size=64):
    return U.EndpointDescriptor(address=address, attributes=attributes,
                                max_packet_size=max_packet_size)


def make_config(*, comm_class=0x02, comm_subclass=0x02, comm_protocol=0x00,
                data_class=0x0A, interrupt=True, bulk_in=True, bulk_out=True,
                max_packet_size=64, comm_endpoint=0x81, comm_max_packet=8):
    comm_eps = []
    if interrupt:
        comm_eps.append(make_endpoint(comm_endpoint, 0x03, comm_max_packet))
    data_eps = []
    if bulk_out:
        data_eps.append(make_endpoint(U.BULK_OUT_ENDPOINT, 0x02, max_packet_size))
    if bulk_in:
        data_eps.append(make_endpoint(U.BULK_IN_ENDPOINT, 0x02, max_packet_size))
    return U.ConfigurationDescriptor(value=1, interfaces=(
        U.InterfaceDescriptor(number=0, alt_setting=0, class_code=comm_class,
                              subclass_code=comm_subclass,
                              protocol_code=comm_protocol,
                              endpoints=tuple(comm_eps)),
        U.InterfaceDescriptor(number=1, alt_setting=0, class_code=data_class,
                              subclass_code=0, protocol_code=0,
                              endpoints=tuple(data_eps)),
        U.InterfaceDescriptor(number=2, alt_setting=0, class_code=0xFF,
                              subclass_code=0, protocol_code=0,
                              endpoints=(make_endpoint(0x83, 0x02, 64),)),
    ))


def make_device(**kwargs):
    return FakeDevice(**kwargs)
