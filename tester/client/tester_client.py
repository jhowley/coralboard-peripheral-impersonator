#!/usr/bin/env python3
"""Minimal TESTER fixture client (stdlib only).

Safety defaults:
  * exact USB iSerialNumber required; no globs, bus/address or product match;
  * PING is always the first command and must answer
    "OK PING TESTER fw=<x.y.z>" before anything else is sent;
  * the default action sends no bus command at all (PING + STATUS only);
  * one explicit bus command per invocation, at most 32 bytes.

Transport: /dev/ttyACM* selected by serial through sysfs, or (on hosts with
no CDC ACM driver) the existing coralbench.usb_link.USBLink over libusb. That
module is imported read-only from --bench-src; this project never modifies it.
"""

from __future__ import annotations

import argparse
import fcntl
import glob
import os
import re
import select
import struct
import sys
import termios
import time

# Default to this repository's src/ directory (tester/client/../..), so the
# libusb fallback works from a checkout without a machine-specific path.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_BENCH_SRC = os.path.join(_REPO_ROOT, "src")
PING_RE = re.compile(r"^OK PING TESTER fw=(\d+\.\d+\.\d+)$")
SERIAL_RE = re.compile(r"^[0-9A-Fa-f]{16}$")
MAX_LINE = 1024


class LinkError(Exception):
    pass


# --------------------------------------------------------------- tty link --

def _tty_serial(path: str):
    tty = os.path.basename(path)
    base = f"/sys/class/tty/{tty}/device"
    node = os.path.realpath(base)
    for _ in range(4):
        candidate = os.path.join(node, "serial")
        try:
            with open(candidate, "r", encoding="ascii") as fh:
                return fh.read().strip()
        except OSError:
            pass
        parent = os.path.dirname(node)
        if parent == node:
            break
        node = parent
    return None


def find_tty(serial: str) -> str:
    matches = []
    seen = []
    for path in sorted(glob.glob("/dev/ttyACM*")):
        candidate = _tty_serial(path)
        seen.append(f"{path}={candidate!r}")
        if candidate == serial:
            matches.append(path)
    if not matches:
        raise LinkError(
            f"no /dev/ttyACM* device reports serial {serial!r} (saw {', '.join(seen) or 'none'})")
    if len(matches) > 1:
        raise LinkError(
            f"{len(matches)} devices report serial {serial!r}; refusing to choose")
    return matches[0]


class TtyLink:
    def __init__(self, serial: str, timeout_s: float):
        self.serial = serial
        self.timeout_s = timeout_s
        self.path = find_tty(serial)
        self.fd = None
        self._buf = bytearray()

    def open(self):
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            raise LinkError(f"cannot open {self.path}: {exc}") from exc
        try:
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0                      # iflag
            attrs[1] = 0                      # oflag
            attrs[3] = 0                      # lflag
            attrs[2] = (attrs[2] & ~(termios.CSIZE | termios.PARENB |
                                     termios.CSTOPB)) | termios.CS8 | \
                termios.CREAD | termios.CLOCAL
            attrs[4] = termios.B115200
            attrs[5] = termios.B115200
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIOFLUSH)
            bits = getattr(termios, "TIOCM_DTR", 0x002) | \
                getattr(termios, "TIOCM_RTS", 0x004)
            fcntl.ioctl(fd, termios.TIOCMBIS, struct.pack("I", bits))
        except OSError as exc:
            os.close(fd)
            raise LinkError(f"cannot configure {self.path}: {exc}") from exc
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def send_line(self, line: str):
        payload = line.encode("ascii") + b"\n"
        deadline = time.monotonic() + self.timeout_s
        sent = 0
        while sent < len(payload):
            try:
                written = os.write(self.fd, payload[sent:])
            except BlockingIOError:
                written = 0
            except OSError as exc:
                raise LinkError(f"write failed: {exc}") from exc
            sent += written
            if sent < len(payload):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LinkError("write timed out")
                select.select([], [self.fd], [], remaining)

    def read_line(self):
        deadline = time.monotonic() + self.timeout_s
        while True:
            idx = self._buf.find(b"\n")
            if idx >= 0:
                raw = bytes(self._buf[:idx]).rstrip(b"\r")
                del self._buf[:idx + 1]
                if len(raw) > MAX_LINE:
                    raise LinkError("reply line too long")
                try:
                    return raw.decode("ascii")
                except UnicodeDecodeError as exc:
                    raise LinkError("non-ASCII reply") from exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([self.fd], [], [], remaining)
            if not ready:
                continue
            try:
                chunk = os.read(self.fd, 4096)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise LinkError(f"read failed: {exc}") from exc
            if not chunk:
                raise LinkError("serial device closed")
            self._buf.extend(chunk)
            if len(self._buf) > MAX_LINE * 2:
                raise LinkError("no newline in reply")

    def close(self):
        if self.fd is not None:
            try:
                termios.tcflush(self.fd, termios.TCIOFLUSH)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None


# --------------------------------------------------------------- usb link --

class UsbLinkAdapter:
    def __init__(self, serial: str, timeout_s: float, bench_src: str):
        self.serial = serial
        self.timeout_s = timeout_s
        self.bench_src = bench_src
        self.link = None

    def open(self):
        if not os.path.isdir(self.bench_src):
            raise LinkError(f"bench source not found: {self.bench_src}")
        if self.bench_src not in sys.path:
            sys.path.insert(0, self.bench_src)
        try:
            from coralbench.usb_link import USBLink  # read-only reuse
        except Exception as exc:  # pragma: no cover - host dependent
            raise LinkError(f"cannot import coralbench.usb_link: {exc}") from exc
        self.link = USBLink(self.serial, timeout_s=self.timeout_s)
        self.link.open()
        self.path = "libusb 2e8a:000a"
        return self

    def send_line(self, line: str):
        self.link.send_line(line)

    def read_line(self):
        return self.link.read_line(self.timeout_s)

    def close(self):
        if self.link is not None:
            self.link.close()
            self.link = None


# -------------------------------------------------------------------- cli --

def validate_hex(value: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{2,64}", value):
        raise argparse.ArgumentTypeError("hex must be 2..64 digits (1..32 bytes)")
    if len(value) % 2:
        raise argparse.ArgumentTypeError("hex must have an even number of digits")
    return value


def validate_count(value: str) -> int:
    try:
        count = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("count must be a decimal integer") from exc
    if not 1 <= count <= 32:
        raise argparse.ArgumentTypeError("count must be 1..32")
    return count


def build_parser():
    p = argparse.ArgumentParser(description="TESTER identity check / one bounded I2C command")
    p.add_argument("--serial", required=True,
                   help="exact Pico USB serial, e.g. E660000000000001")
    p.add_argument("--transport", choices=("auto", "tty", "usb"), default="auto")
    p.add_argument("--bench-src", default=DEFAULT_BENCH_SRC,
                   help="read-only path to coralbench source for libusb fallback")
    p.add_argument("--fw", default="1.0.0", help="expected TESTER firmware version")
    p.add_argument("--timeout", type=float, default=2.0,
                   help="USB line timeout in seconds (default 2.0)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--write", metavar="HEX",
                   help="send W <hex> after the identity check")
    g.add_argument("--read", metavar="N", type=validate_count,
                   help="send R <n> after the identity check")
    g.add_argument("--write-read", nargs=2, metavar=("HEX", "N"),
                   help="send WR <hex> <n> after the identity check")
    return p


def open_link(args):
    serial = args.serial
    if not SERIAL_RE.fullmatch(serial):
        raise LinkError("serial must be 16 hex digits, exactly as printed on the board")
    if not 0 < args.timeout <= 60:
        raise LinkError("timeout must be in (0, 60]")
    if args.transport in ("auto", "tty"):
        try:
            return TtyLink(serial, args.timeout).open()
        except LinkError as exc:
            if args.transport == "tty":
                raise
            tty_error = exc
    else:
        tty_error = None
    if args.transport in ("auto", "usb"):
        try:
            return UsbLinkAdapter(serial, args.timeout, args.bench_src).open()
        except Exception as exc:
            if args.transport == "usb":
                raise LinkError(str(exc)) from exc
            raise LinkError(f"tty: {tty_error}; usb: {exc}") from exc
    raise LinkError(str(tty_error))


def main() -> int:
    args = build_parser().parse_args()

    # Validate and build the optional single bus command before touching any
    # device. Default (no flag) stays None: PING + STATUS only, no bus call.
    cmd = None
    if args.write is not None:
        cmd = f"W {args.write}"
    elif args.read is not None:
        cmd = f"R {args.read}"
    elif args.write_read is not None:
        hex_value, count = args.write_read
        try:
            hex_value = validate_hex(hex_value)
            count = validate_count(count)
        except argparse.ArgumentTypeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        cmd = f"WR {hex_value} {count}"

    link = None
    try:
        link = open_link(args)
        print(f"transport: {getattr(link, 'path', '?')} serial={args.serial}",
              file=sys.stderr)

        link.send_line("PING")
        line = link.read_line()
        if line is None:
            raise LinkError("no PING reply within timeout")
        print(line)
        m = PING_RE.fullmatch(line)
        if not m:
            raise LinkError(f"refusing: not TESTER firmware (PING -> {line!r})")
        if m.group(1) != args.fw:
            raise LinkError(
                f"refusing: TESTER firmware {m.group(1)} != expected {args.fw}")

        link.send_line("STATUS")
        status = link.read_line()
        if status is None:
            raise LinkError("no STATUS reply within timeout")
        print(status)
        if not status.startswith("OK STATUS role=TESTER "):
            raise LinkError(f"refusing: bad STATUS reply {status!r}")

        if cmd is None:
            return 0

        link.send_line(cmd)
        reply = link.read_line()
        if reply is None:
            raise LinkError(f"no reply to {cmd!r} within timeout")
        print(reply)
        return 3 if reply.startswith("ERR ") else 0
    except LinkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if link is not None:
            link.close()


if __name__ == "__main__":
    sys.exit(main())
