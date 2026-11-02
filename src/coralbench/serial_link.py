"""Explicit-path serial transport for the Pico USB CDC link.

Stdlib only (os/termios/select). No auto-discovery: the caller must pass an
existing character-device path. Opening configures termios but writes no data
and flushes nothing, so attaching mid-lease cannot inject a command or reset
the board.
"""

from __future__ import annotations

import os
import select
import termios
import time
from dataclasses import dataclass, field

MAX_LINE = 1024
DEFAULT_BAUD = 115200

_BAUD_CONSTANTS = {
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
    230400: termios.B230400,
}


class SerialLinkError(RuntimeError):
    pass


def check_device_path(path: str) -> str:
    """Validate an explicitly supplied device path. Never expands patterns."""
    if not isinstance(path, str) or not path.strip():
        raise SerialLinkError("explicit device path required (e.g. /dev/ttyACM0)")
    if any(ch in path for ch in "*?["):
        raise SerialLinkError(f"glob pattern not allowed: {path!r} (need one explicit device path)")
    if not path.startswith("/"):
        raise SerialLinkError(f"absolute device path required: {path!r}")
    if not os.path.exists(path):
        raise SerialLinkError(f"device path does not exist: {path!r}")
    if os.path.isdir(path):
        raise SerialLinkError(f"device path is a directory: {path!r}")
    return path


@dataclass
class PosixSerial:
    path: str
    baud: int = DEFAULT_BAUD
    timeout_s: float = 1.0
    fd: int = -1
    _buf: bytes = field(default=b"", repr=False)

    def open(self) -> "PosixSerial":
        if self.fd >= 0:
            raise SerialLinkError("already open")
        check_device_path(self.path)
        if self.baud not in _BAUD_CONSTANTS:
            raise SerialLinkError(f"unsupported baud {self.baud}")
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            raise SerialLinkError(f"cannot open {self.path!r}: {exc.strerror or exc}") from exc
        try:
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0  # iflag: no software flow control, no CR/NL munging
            attrs[1] = 0  # oflag: no post-processing
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[3] = 0  # lflag: raw, no echo, no canonical mode
            speed = _BAUD_CONSTANTS[self.baud]
            attrs[4] = speed
            attrs[5] = speed
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except (OSError, termios.error) as exc:
            os.close(fd)
            raise SerialLinkError(f"cannot configure {self.path!r}: {exc}") from exc
        self.fd = fd
        self._buf = b""
        return self

    def write_line(self, line: str, timeout_s: float = 1.0) -> None:
        if self.fd < 0:
            raise SerialLinkError("port is not open")
        if not isinstance(line, str):
            raise SerialLinkError("command must be a string")
        if "\n" in line or "\r" in line:
            raise SerialLinkError("command must not contain newlines")
        try:
            payload = line.encode("ascii") + b"\n"
        except UnicodeEncodeError as exc:
            raise SerialLinkError("command must be ASCII") from exc
        if len(payload) > MAX_LINE:
            raise SerialLinkError(f"command too long ({len(payload)} > {MAX_LINE} bytes)")

        deadline = time.monotonic() + timeout_s
        sent = 0
        while sent < len(payload):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SerialLinkError("write timeout")
            try:
                _, writable, _ = select.select([], [self.fd], [], remaining)
            except InterruptedError:
                continue
            if not writable:
                raise SerialLinkError("write timeout")
            try:
                sent += os.write(self.fd, payload[sent:])
            except BlockingIOError:
                continue
            except OSError as exc:
                raise SerialLinkError(f"write failed (disconnect?): {exc.strerror or exc}") from exc

    def read_line(self, timeout_s: float = 1.0) -> str:
        """Return one line without its newline, or None on timeout."""
        if self.fd < 0:
            raise SerialLinkError("port is not open")
        deadline = time.monotonic() + timeout_s
        while True:
            if b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                line = line.rstrip(b"\r")
                if len(line) > MAX_LINE:
                    raise SerialLinkError(f"incoming line too long (> {MAX_LINE} bytes)")
                try:
                    return line.decode("ascii")
                except UnicodeDecodeError as exc:
                    raise SerialLinkError("non-ASCII bytes on the link") from exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                readable, _, _ = select.select([self.fd], [], [], remaining)
            except InterruptedError:
                continue
            if not readable:
                return None
            try:
                chunk = os.read(self.fd, 4096)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise SerialLinkError(f"read failed (disconnect?): {exc.strerror or exc}") from exc
            if not chunk:
                raise SerialLinkError("device disconnected (EOF)")
            self._buf += chunk
            if len(self._buf) > MAX_LINE * 4 and b"\n" not in self._buf:
                raise SerialLinkError("no newline within buffer budget (garbled link?)")

    def close(self) -> None:
        if self.fd >= 0:
            try:
                os.close(self.fd)
            finally:
                self.fd = -1
                self._buf = b""

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc_info):
        self.close()
