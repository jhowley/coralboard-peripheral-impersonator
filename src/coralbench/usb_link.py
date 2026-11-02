"""Userspace USB CDC transport over libusb-1.0 (ctypes, stdlib only).

Why this exists: the Coralboard Yocto image has libusb but no bound CDC ACM
driver, so the Pico's /dev/ttyACM* node never appears. This module speaks the
same newline protocol directly over the observed CDC data endpoints.

Hard rules, enforced in code:

* the only selection input is an exact USB iSerialNumber; no bus/address,
  path or glob can select a device;
* more than one matching serial is ambiguous and is refused;
* the observed descriptor layout (2e8a:000a, interface 0 CDC-ACM with
  interrupt IN 0x81, interface 1 CDC-Data with bulk OUT 0x02 / bulk IN 0x82,
  64-byte packets) is verified before claiming; any mismatch fails closed;
* interfaces 0 and 1 only — the vendor "reset" interface 2 is never claimed;
* an active kernel driver is refused, never detached;
* no device reset, no bootloader entry, no configuration change.

reads and writes are deadline-bounded and tolerate libusb partial transfers
(a timeout can return bytes). A fatal transfer error poisons the link; CLI
sessions additionally poison the controller on any timeout.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import math
import struct
import time
from dataclasses import dataclass
from typing import Optional, Sequence

from .serial_link import MAX_LINE, SerialLinkError

DEVICE_VID = 0x2E8A
DEVICE_PID = 0x000A
COMM_INTERFACE = 0
DATA_INTERFACE = 1
BULK_OUT_ENDPOINT = 0x02
BULK_IN_ENDPOINT = 0x82
INTERRUPT_IN_ENDPOINT = 0x81
MAX_PACKET = 64
READ_CHUNK = 4096
MAX_SERIAL_LENGTH = 128

LIBUSB_SUCCESS = 0
LIBUSB_ERROR_IO = -1
LIBUSB_ERROR_INVALID_PARAM = -2
LIBUSB_ERROR_ACCESS = -3
LIBUSB_ERROR_NO_DEVICE = -4
LIBUSB_ERROR_BUSY = -6
LIBUSB_ERROR_TIMEOUT = -7
LIBUSB_ERROR_OVERFLOW = -8
LIBUSB_ERROR_PIPE = -9
LIBUSB_ERROR_INTERRUPTED = -10
LIBUSB_ERROR_NOT_SUPPORTED = -12

_ERROR_NAMES = {
    0: "LIBUSB_SUCCESS",
    -1: "LIBUSB_ERROR_IO",
    -2: "LIBUSB_ERROR_INVALID_PARAM",
    -3: "LIBUSB_ERROR_ACCESS",
    -4: "LIBUSB_ERROR_NO_DEVICE",
    -5: "LIBUSB_ERROR_NOT_FOUND",
    -6: "LIBUSB_ERROR_BUSY",
    -7: "LIBUSB_ERROR_TIMEOUT",
    -8: "LIBUSB_ERROR_OVERFLOW",
    -9: "LIBUSB_ERROR_PIPE",
    -10: "LIBUSB_ERROR_INTERRUPTED",
    -11: "LIBUSB_ERROR_NO_MEM",
    -12: "LIBUSB_ERROR_NOT_SUPPORTED",
    -99: "LIBUSB_ERROR_OTHER",
}

_CDC_SET_LINE_CODING = 0x20
_CDC_SET_CONTROL_LINE_STATE = 0x22
_CDC_LINE_CODING = struct.pack("<IBBB", 115200, 0, 0, 8)


class USBLinkError(SerialLinkError):
    """Link-layer failure; a subclass so the controller poisons uniformly."""


def libusb_error_name(rc: int) -> str:
    return _ERROR_NAMES.get(rc, f"libusb error {rc}")


def timeout_to_ms(seconds: float) -> int:
    """libusb treats 0 as 'forever'; never emit 0 from a bounded deadline."""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return 1
    if not math.isfinite(value) or value <= 0:
        return 1
    return max(1, min(int(math.ceil(value * 1000)), 2**31 - 1))


def _clamp_ms(timeout_ms: int) -> int:
    return max(1, min(int(timeout_ms), 2**31 - 1))


def check_serial_number(value: str) -> str:
    """Validate an exact iSerialNumber selector. Never a pattern."""
    if not isinstance(value, str):
        raise USBLinkError("explicit USB serial number required (a string)")
    if not value or value != value.strip():
        raise USBLinkError("USB serial number must be non-empty with no padding")
    if len(value) > MAX_SERIAL_LENGTH:
        raise USBLinkError(f"USB serial number too long (> {MAX_SERIAL_LENGTH})")
    for ch in value:
        if ord(ch) < 0x21 or ord(ch) > 0x7E:
            raise USBLinkError(f"USB serial number has a non-printable character: {value!r}")
    if any(ch in value for ch in "*?[]/\\:"):
        raise USBLinkError(f"USB serial number must be literal, not a pattern or path: {value!r}")
    return value


@dataclass(frozen=True)
class EndpointDescriptor:
    address: int
    attributes: int
    max_packet_size: int

    @property
    def transfer_type(self) -> int:
        return self.attributes & 0x03

    @property
    def is_in(self) -> bool:
        return bool(self.address & 0x80)


@dataclass(frozen=True)
class InterfaceDescriptor:
    number: int
    alt_setting: int
    class_code: int
    subclass_code: int
    protocol_code: int
    endpoints: Sequence[EndpointDescriptor]


@dataclass(frozen=True)
class ConfigurationDescriptor:
    value: int
    interfaces: Sequence[InterfaceDescriptor]


@dataclass(frozen=True)
class DeviceDescriptor:
    vendor_id: int
    product_id: int
    serial_index: int


def _find_endpoint(interface, address, transfer_type, *, label):
    for ep in interface.endpoints:
        if ep.address == address and ep.transfer_type == transfer_type:
            return ep
    raise USBLinkError(
        f"descriptor mismatch: interface {interface.number} has no {label} "
        f"endpoint 0x{address:02x}; refusing to guess")


def verify_config(config: ConfigurationDescriptor) -> tuple:
    """Verify the observed CDC layout. Returns (bulk_in, bulk_out) addresses."""
    if not isinstance(config, ConfigurationDescriptor):
        raise USBLinkError("descriptor mismatch: no configuration descriptor")
    interfaces = {i.number: i for i in config.interfaces if i.alt_setting == 0}
    comm = interfaces.get(COMM_INTERFACE)
    data = interfaces.get(DATA_INTERFACE)
    if comm is None:
        raise USBLinkError("descriptor mismatch: interface 0 (CDC communications) missing")
    if data is None:
        raise USBLinkError("descriptor mismatch: interface 1 (CDC data) missing")
    if (comm.class_code, comm.subclass_code, comm.protocol_code) != (0x02, 0x02, 0x00):
        raise USBLinkError(
            "descriptor mismatch: interface 0 is not CDC-ACM "
            f"(class={comm.class_code:#04x} subclass={comm.subclass_code:#04x} "
            f"protocol={comm.protocol_code:#04x})")
    if data.class_code != 0x0A:
        raise USBLinkError(
            f"descriptor mismatch: interface 1 is not CDC data (class={data.class_code:#04x})")
    _find_endpoint(comm, INTERRUPT_IN_ENDPOINT, 0x03, label="interrupt IN")
    bulk_in = _find_endpoint(data, BULK_IN_ENDPOINT, 0x02, label="bulk IN")
    bulk_out = _find_endpoint(data, BULK_OUT_ENDPOINT, 0x02, label="bulk OUT")
    if not bulk_in.is_in or bulk_out.is_in:
        raise USBLinkError("descriptor mismatch: bulk endpoint directions are swapped")
    for ep in (bulk_in, bulk_out):
        if ep.max_packet_size != MAX_PACKET:
            raise USBLinkError(
                f"descriptor mismatch: endpoint 0x{ep.address:02x} packet size "
                f"{ep.max_packet_size} != {MAX_PACKET}")
    return BULK_IN_ENDPOINT, BULK_OUT_ENDPOINT


# -- ctypes ABI -----------------------------------------------------------

class LibusbDeviceDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8),
        ("bDescriptorType", ctypes.c_uint8),
        ("bcdUSB", ctypes.c_uint16),
        ("bDeviceClass", ctypes.c_uint8),
        ("bDeviceSubClass", ctypes.c_uint8),
        ("bDeviceProtocol", ctypes.c_uint8),
        ("bMaxPacketSize0", ctypes.c_uint8),
        ("idVendor", ctypes.c_uint16),
        ("idProduct", ctypes.c_uint16),
        ("bcdDevice", ctypes.c_uint16),
        ("iManufacturer", ctypes.c_uint8),
        ("iProduct", ctypes.c_uint8),
        ("iSerialNumber", ctypes.c_uint8),
        ("bNumConfigurations", ctypes.c_uint8),
    ]


class LibusbEndpointDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8),
        ("bDescriptorType", ctypes.c_uint8),
        ("bEndpointAddress", ctypes.c_uint8),
        ("bmAttributes", ctypes.c_uint8),
        ("wMaxPacketSize", ctypes.c_uint16),
        ("bInterval", ctypes.c_uint8),
        ("bRefresh", ctypes.c_uint8),
        ("bSynchAddress", ctypes.c_uint8),
        ("extra", ctypes.POINTER(ctypes.c_ubyte)),
        ("extra_length", ctypes.c_int),
    ]


class LibusbInterfaceDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8),
        ("bDescriptorType", ctypes.c_uint8),
        ("bInterfaceNumber", ctypes.c_uint8),
        ("bAlternateSetting", ctypes.c_uint8),
        ("bNumEndpoints", ctypes.c_uint8),
        ("bInterfaceClass", ctypes.c_uint8),
        ("bInterfaceSubClass", ctypes.c_uint8),
        ("bInterfaceProtocol", ctypes.c_uint8),
        ("iInterface", ctypes.c_uint8),
        ("endpoint", ctypes.POINTER(LibusbEndpointDescriptor)),
        ("extra", ctypes.POINTER(ctypes.c_ubyte)),
        ("extra_length", ctypes.c_int),
    ]


class LibusbInterface(ctypes.Structure):
    _fields_ = [
        ("altsetting", ctypes.POINTER(LibusbInterfaceDescriptor)),
        ("num_altsetting", ctypes.c_int),
    ]


class LibusbConfigDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8),
        ("bDescriptorType", ctypes.c_uint8),
        ("wTotalLength", ctypes.c_uint16),
        ("bNumInterfaces", ctypes.c_uint8),
        ("bConfigurationValue", ctypes.c_uint8),
        ("iConfiguration", ctypes.c_uint8),
        ("bmAttributes", ctypes.c_uint8),
        ("MaxPower", ctypes.c_uint8),
        ("interface", ctypes.POINTER(LibusbInterface)),
        ("extra", ctypes.POINTER(ctypes.c_ubyte)),
        ("extra_length", ctypes.c_int),
    ]


def config_from_ctypes(cfg: LibusbConfigDescriptor) -> ConfigurationDescriptor:
    """Convert libusb's C config descriptor tree into plain frozen dataclasses."""
    interfaces = []
    for i in range(cfg.bNumInterfaces):
        iface = cfg.interface[i]
        for a in range(iface.num_altsetting):
            alt = iface.altsetting[a]
            endpoints = []
            for e in range(alt.bNumEndpoints):
                raw = alt.endpoint[e]
                endpoints.append(EndpointDescriptor(
                    address=raw.bEndpointAddress,
                    attributes=raw.bmAttributes,
                    max_packet_size=raw.wMaxPacketSize & 0x7FF))
            interfaces.append(InterfaceDescriptor(
                number=alt.bInterfaceNumber,
                alt_setting=alt.bAlternateSetting,
                class_code=alt.bInterfaceClass,
                subclass_code=alt.bInterfaceSubClass,
                protocol_code=alt.bInterfaceProtocol,
                endpoints=tuple(endpoints)))
    return ConfigurationDescriptor(value=cfg.bConfigurationValue,
                                   interfaces=tuple(interfaces))


class _DeviceList:
    def __init__(self, backend, pointer, count):
        self._backend = backend
        self._pointer = pointer
        self._devices = tuple(pointer[i] for i in range(count))
        self.disposed = False

    def __iter__(self):
        return iter(self._devices)

    def __len__(self):
        return len(self._devices)

    def dispose(self):
        if not self.disposed:
            self.disposed = True
            self._backend.lib.libusb_free_device_list(self._pointer, 1)


class LibUSBBackend:
    """Thin ctypes binding. Transfers return libusb's raw (rc, count) values."""

    def __init__(self, lib):
        self.lib = lib
        self._bind()

    @classmethod
    def load(cls, path: Optional[str] = None) -> "LibUSBBackend":
        name = path or ctypes.util.find_library("usb-1.0") or "libusb-1.0.so.0"
        try:
            lib = ctypes.CDLL(name)
        except OSError as exc:
            raise USBLinkError(
                f"cannot load libusb-1.0 ({name!r}): {exc.strerror or exc}") from exc
        return cls(lib)

    def _bind(self):
        specs = [
            ("libusb_init", [ctypes.POINTER(ctypes.c_void_p)], ctypes.c_int),
            ("libusb_exit", [ctypes.c_void_p], None),
            ("libusb_get_device_list",
             [ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))],
             ctypes.c_ssize_t),
            ("libusb_free_device_list",
             [ctypes.POINTER(ctypes.c_void_p), ctypes.c_int], None),
            ("libusb_get_device_descriptor",
             [ctypes.c_void_p, ctypes.POINTER(LibusbDeviceDescriptor)], ctypes.c_int),
            ("libusb_open",
             [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], ctypes.c_int),
            ("libusb_close", [ctypes.c_void_p], None),
            ("libusb_get_string_descriptor_ascii",
             [ctypes.c_void_p, ctypes.c_uint8, ctypes.POINTER(ctypes.c_ubyte),
              ctypes.c_int], ctypes.c_int),
            ("libusb_get_config_descriptor",
             [ctypes.c_void_p, ctypes.c_uint8,
              ctypes.POINTER(ctypes.POINTER(LibusbConfigDescriptor))], ctypes.c_int),
            ("libusb_free_config_descriptor",
             [ctypes.POINTER(LibusbConfigDescriptor)], None),
            ("libusb_kernel_driver_active", [ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
            ("libusb_claim_interface", [ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
            ("libusb_release_interface", [ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
            ("libusb_control_transfer",
             [ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint16,
              ctypes.c_uint16, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint16,
              ctypes.c_uint], ctypes.c_int),
            ("libusb_bulk_transfer",
             [ctypes.c_void_p, ctypes.c_ubyte, ctypes.POINTER(ctypes.c_ubyte),
              ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_uint], ctypes.c_int),
        ]
        for name, argtypes, restype in specs:
            try:
                fn = getattr(self.lib, name)
            except AttributeError as exc:
                raise USBLinkError(f"libusb is missing required symbol {name}") from exc
            fn.argtypes = argtypes
            fn.restype = restype

    # -- context / enumeration -----------------------------------------
    def init(self):
        ctx = ctypes.c_void_p()
        rc = self.lib.libusb_init(ctypes.byref(ctx))
        if rc != 0:
            raise USBLinkError(f"libusb_init failed: {rc} ({libusb_error_name(rc)})")
        return ctx

    def exit(self, ctx):
        if ctx is not None:
            self.lib.libusb_exit(ctx)

    def list_devices(self, ctx):
        pointer = ctypes.POINTER(ctypes.c_void_p)()
        count = self.lib.libusb_get_device_list(ctx, ctypes.byref(pointer))
        if count < 0:
            raise USBLinkError(
                f"libusb_get_device_list failed: {count} ({libusb_error_name(count)})")
        return _DeviceList(self, pointer, int(count))

    def device_descriptor(self, device):
        desc = LibusbDeviceDescriptor()
        rc = self.lib.libusb_get_device_descriptor(device, ctypes.byref(desc))
        if rc != 0:
            raise USBLinkError(
                f"libusb_get_device_descriptor failed: {rc} ({libusb_error_name(rc)})")
        return DeviceDescriptor(vendor_id=desc.idVendor, product_id=desc.idProduct,
                                serial_index=desc.iSerialNumber)

    def open(self, ctx, device):
        handle = ctypes.c_void_p()
        rc = self.lib.libusb_open(device, ctypes.byref(handle))
        if rc != 0:
            raise USBLinkError(f"libusb_open failed: {rc} ({libusb_error_name(rc)})")
        return handle

    def close(self, handle):
        if handle is not None:
            self.lib.libusb_close(handle)

    def string_descriptor(self, handle, index):
        buf = (ctypes.c_ubyte * 255)()
        rc = self.lib.libusb_get_string_descriptor_ascii(handle, index, buf, 255)
        if rc < 0:
            raise USBLinkError(
                f"libusb_get_string_descriptor_ascii({index}) failed: "
                f"{rc} ({libusb_error_name(rc)})")
        try:
            return bytes(buf[:rc]).decode("ascii")
        except UnicodeDecodeError as exc:
            raise USBLinkError(f"USB serial descriptor {index} is not ASCII") from exc

    # -- configuration --------------------------------------------------
    def config_descriptor(self, device):
        pointer = ctypes.POINTER(LibusbConfigDescriptor)()
        rc = self.lib.libusb_get_config_descriptor(device, 0, ctypes.byref(pointer))
        if rc != 0:
            raise USBLinkError(
                f"libusb_get_config_descriptor failed: {rc} ({libusb_error_name(rc)})")
        try:
            return config_from_ctypes(pointer.contents)
        finally:
            self.lib.libusb_free_config_descriptor(pointer)

    def kernel_driver_active(self, handle, interface):
        return self.lib.libusb_kernel_driver_active(handle, interface)

    def claim_interface(self, handle, interface):
        rc = self.lib.libusb_claim_interface(handle, interface)
        if rc != 0:
            raise USBLinkError(
                f"libusb_claim_interface({interface}) failed: "
                f"{rc} ({libusb_error_name(rc)})")

    def release_interface(self, handle, interface):
        rc = self.lib.libusb_release_interface(handle, interface)
        if rc != 0:
            raise USBLinkError(
                f"libusb_release_interface({interface}) failed: "
                f"{rc} ({libusb_error_name(rc)})")

    # -- transfers ------------------------------------------------------
    def control_transfer(self, handle, request_type, request, value, index,
                         data, timeout_ms):
        if data is None:
            buf = None
            length = 0
        else:
            buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
            length = len(data)
        return self.lib.libusb_control_transfer(
            handle, request_type, request, value, index, buf, length,
            _clamp_ms(timeout_ms))

    def bulk_transfer(self, handle, endpoint, data, length, timeout_ms):
        count = ctypes.c_int(0)
        if data is None:
            buf = (ctypes.c_ubyte * length)()
            rc = self.lib.libusb_bulk_transfer(
                handle, endpoint, buf, length, ctypes.byref(count),
                _clamp_ms(timeout_ms))
            return rc, count.value, bytes(buf[:count.value]) if count.value > 0 else b""
        payload = bytes(data)
        buf = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
        rc = self.lib.libusb_bulk_transfer(
            handle, endpoint, buf, len(payload), ctypes.byref(count),
            _clamp_ms(timeout_ms))
        return rc, count.value, b""


def load_backend(path: Optional[str] = None) -> LibUSBBackend:
    return LibUSBBackend.load(path)


class USBLink:
    """Line-oriented CDC transport selected by an exact USB serial number."""

    def __init__(self, serial_number: str, *, timeout_s: float = 2.0,
                 backend=None):
        self.serial_number = check_serial_number(serial_number)
        if not 0 < timeout_s <= 60:
            raise USBLinkError("timeout_s must be in (0, 60]")
        self.timeout_s = float(timeout_s)
        self._backend = backend
        self._ctx = None
        self._handle = None
        self._claimed = []
        self._buf = b""
        self._fatal = None
        self.cleanup_error = None

    # -- lifecycle ------------------------------------------------------
    def open(self) -> "USBLink":
        if self._handle is not None:
            raise USBLinkError("USB link is already open")
        if self._fatal:
            raise USBLinkError(f"USB link poisoned: {self._fatal}")
        if self._backend is None:
            self._backend = load_backend()
        self._ctx = self._backend.init()
        try:
            self._handle = self._select_device()
            for interface in (COMM_INTERFACE, DATA_INTERFACE):
                active = self._backend.kernel_driver_active(self._handle, interface)
                if active == 1:
                    raise USBLinkError(
                        f"kernel driver is bound to interface {interface}; "
                        "refusing to detach it automatically")
                if active < 0:
                    raise USBLinkError(
                        f"cannot determine kernel driver state on interface {interface}: "
                        f"{active} ({libusb_error_name(active)}); refusing to claim")
            for interface in (COMM_INTERFACE, DATA_INTERFACE):
                self._backend.claim_interface(self._handle, interface)
                self._claimed.append(interface)
            self._configure_cdc()
        except BaseException:
            self._cleanup()
            raise
        self._buf = b""
        return self

    def _select_device(self):
        listing = self._backend.list_devices(self._ctx)
        opened = []
        selected = None
        try:
            candidates = []
            for device in listing:
                desc = self._backend.device_descriptor(device)
                if desc.vendor_id == DEVICE_VID and desc.product_id == DEVICE_PID:
                    candidates.append((device, desc))
            if not candidates:
                raise USBLinkError(
                    "no USB device with id 2e8a:000a is present "
                    "(expected the Pico CDC device)")
            seen = []
            matches = []
            for device, desc in candidates:
                handle = self._backend.open(self._ctx, device)
                opened.append(handle)
                serial = None
                if desc.serial_index:
                    try:
                        serial = self._backend.string_descriptor(handle, desc.serial_index)
                    except USBLinkError:
                        serial = None
                seen.append(serial)
                if serial == self.serial_number:
                    matches.append((handle, device))
            if not matches:
                shown = ", ".join(repr(s) for s in seen)
                raise USBLinkError(
                    f"no 2e8a:000a device with serial {self.serial_number!r} "
                    f"(saw {shown or 'none'}); refusing any other device")
            if len(matches) > 1:
                raise USBLinkError(
                    f"ambiguous device selection: {len(matches)} 2e8a:000a devices "
                    f"report serial {self.serial_number!r}; refusing to choose")
            selected = matches[0][0]
            config = self._backend.config_descriptor(matches[0][1])
            verify_config(config)
        except BaseException:
            for handle in opened:
                try:
                    self._backend.close(handle)
                except Exception:
                    pass
            raise
        else:
            for handle in opened:
                if handle is not selected:
                    self._backend.close(handle)
            return selected
        finally:
            listing.dispose()

    def _configure_cdc(self):
        timeout_ms = timeout_to_ms(self.timeout_s)
        rc = self._backend.control_transfer(
            self._handle, 0x21, _CDC_SET_LINE_CODING, 0, COMM_INTERFACE,
            _CDC_LINE_CODING, timeout_ms)
        if rc != len(_CDC_LINE_CODING):
            raise USBLinkError(
                f"CDC SET_LINE_CODING failed: {rc} ({libusb_error_name(rc)})")
        rc = self._backend.control_transfer(
            self._handle, 0x21, _CDC_SET_CONTROL_LINE_STATE, 3, COMM_INTERFACE,
            None, timeout_ms)
        if rc != 0:
            raise USBLinkError(
                f"CDC SET_CONTROL_LINE_STATE failed: {rc} ({libusb_error_name(rc)})")

    def _cleanup(self):
        errors = []
        for interface in reversed(self._claimed):
            try:
                self._backend.release_interface(self._handle, interface)
            except Exception as exc:  # cleanup is best-effort
                errors.append(f"release {interface}: {exc}")
        self._claimed = []
        if self._handle is not None:
            try:
                self._backend.close(self._handle)
            except Exception as exc:
                errors.append(f"close: {exc}")
        self._handle = None
        if self._ctx is not None:
            try:
                self._backend.exit(self._ctx)
            except Exception as exc:
                errors.append(f"exit: {exc}")
        self._ctx = None
        self._buf = b""
        if errors:
            self.cleanup_error = "; ".join(errors)

    def close(self):
        self._cleanup()

    # -- line protocol --------------------------------------------------
    def _require_open(self):
        if self._fatal:
            raise USBLinkError(f"USB link poisoned: {self._fatal}")
        if self._handle is None:
            raise USBLinkError("USB link is not open")

    def _encode(self, line: str) -> bytes:
        if not isinstance(line, str):
            raise USBLinkError("command must be a string")
        if "\n" in line or "\r" in line:
            raise USBLinkError("command must not contain newlines")
        try:
            payload = line.encode("ascii") + b"\n"
        except UnicodeEncodeError as exc:
            raise USBLinkError("command must be ASCII") from exc
        if len(payload) > MAX_LINE:
            raise USBLinkError(f"command too long ({len(payload)} > {MAX_LINE} bytes)")
        return payload

    def write_line(self, line: str, timeout_s: Optional[float] = None) -> None:
        self._require_open()
        payload = self._encode(line)
        timeout = self.timeout_s if timeout_s is None else float(timeout_s)
        if timeout <= 0:
            raise USBLinkError("write timeout must be positive")
        deadline = time.monotonic() + timeout
        sent = 0
        stalls = 0
        while sent < len(payload):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._fatal = f"write timeout after {sent}/{len(payload)} bytes"
                raise USBLinkError(self._fatal)
            rc, count, _ = self._backend.bulk_transfer(
                self._handle, BULK_OUT_ENDPOINT, payload[sent:], len(payload) - sent,
                timeout_to_ms(remaining))
            if count > 0:
                sent += count
                stalls = 0
            else:
                stalls += 1
                if stalls > 64:
                    self._fatal = "write made no progress"
                    raise USBLinkError("USB write made no progress")
            if rc == LIBUSB_SUCCESS:
                continue
            if rc == LIBUSB_ERROR_TIMEOUT:
                continue
            self._fatal = f"write failed: {libusb_error_name(rc)}"
            raise USBLinkError(f"USB write failed: {rc} ({libusb_error_name(rc)})")
        if sent != len(payload):  # pragma: no cover - guarded above
            self._fatal = "short write"
            raise USBLinkError(f"short write ({sent}/{len(payload)} bytes)")

    def send_line(self, line: str) -> None:
        self.write_line(line)

    def _take_line(self) -> Optional[str]:
        if b"\n" not in self._buf:
            return None
        raw, self._buf = self._buf.split(b"\n", 1)
        raw = raw.rstrip(b"\r")
        if len(raw) > MAX_LINE:
            self._fatal = f"incoming line too long (> {MAX_LINE} bytes)"
            raise USBLinkError(self._fatal)
        try:
            return raw.decode("ascii")
        except UnicodeDecodeError as exc:
            self._fatal = "non-ASCII bytes on the link"
            raise USBLinkError("non-ASCII bytes on the link") from exc

    def read_line(self, timeout_s: Optional[float] = None) -> Optional[str]:
        self._require_open()
        timeout = self.timeout_s if timeout_s is None else float(timeout_s)
        if timeout < 0:
            raise USBLinkError("read timeout must be >= 0")
        deadline = time.monotonic() + timeout
        while True:
            line = self._take_line()
            if line is not None:
                return line
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            rc, count, chunk = self._backend.bulk_transfer(
                self._handle, BULK_IN_ENDPOINT, None, READ_CHUNK,
                timeout_to_ms(remaining))
            if chunk:
                self._buf += chunk
            if len(self._buf) > MAX_LINE * 4 and b"\n" not in self._buf:
                self._fatal = "no newline within buffer budget (unterminated reply)"
                raise USBLinkError(self._fatal)
            if rc == LIBUSB_SUCCESS:
                continue
            if rc == LIBUSB_ERROR_TIMEOUT:
                continue
            self._fatal = f"read failed: {libusb_error_name(rc)}"
            raise USBLinkError(f"USB read failed: {rc} ({libusb_error_name(rc)})")

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc_info):
        self.close()
