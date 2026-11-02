"""Deterministic I2C target simulator for the bench.

Mirrors the firmware core's bounded behaviour: 256-byte register image restored
from a loaded template on every lease, explicit pointer register with the
declared wrap modulus applied to initialisation as well as advance (per byte on
reads), bounded write window, optional post-read pointer increment, epoch id,
and a bounded event ring whose overflow is counted (never silently dropped).

No wall-clock, no randomness: the same op sequence always yields the same
event stream and the same bytes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import List

from .profile import config_crc

DEFAULT_QUEUE_LIMIT = 256
TRACE_DATA_MAX = 8
DRAIN_MAX = 64


class SimulatorError(RuntimeError):
    pass


class NotArmed(SimulatorError):
    pass


class TraceOverflow(SimulatorError):
    pass


@dataclass(frozen=True)
class Event:
    epoch: int
    seq: int
    op: str
    ptr: int
    length: int
    data: bytes
    truncated: bool
    tick: int

    def as_dict(self) -> dict:
        return {
            "epoch": self.epoch,
            "seq": self.seq,
            "op": self.op,
            "ptr": self.ptr,
            "len": self.length,
            "data": self.data.hex(),
            "truncated": self.truncated,
            "tick": self.tick,
        }


class SimulatedTarget:
    """Bounded I2C target. A reload/arm starts a new trace epoch."""

    def __init__(self, profile, queue_limit: int = DEFAULT_QUEUE_LIMIT,
                 image: bytes = None, boot_nonce: int = 0):
        if queue_limit < 1:
            raise ValueError("queue_limit must be >= 1")
        self.profile = profile
        self.boot_nonce = int(boot_nonce) & 0xFFFF
        self.queue_limit = queue_limit
        self.template = bytearray(profile.image if image is None else image)
        if len(self.template) != profile.register_space:
            raise ValueError(
                f"image must be {profile.register_space} bytes, got {len(self.template)}")
        self.image = bytearray(self.template)
        self.pointer = profile.pointer_initial % profile.pointer_wrap
        self.armed = False
        self.sealed = True
        self.dropped = 0
        self.emitted = 0
        self.aborted = 0
        self.drained = 0
        self.rejected_writes = 0
        self._epoch_counter = 0
        self.epoch = self.boot_nonce << 16
        self._queue: List[Event] = []
        self._tick = 0
        self._seq = 0

    # -- state ----------------------------------------------------------
    @property
    def cfg(self) -> int:
        return config_crc(self.profile)

    @property
    def overflowed(self) -> bool:
        return self.dropped > 0

    @property
    def complete_candidate(self) -> bool:
        return (self.sealed and not self.armed and self.dropped == 0
                and self.aborted == 0 and not self._queue and self.emitted > 0
                and self.drained == self.emitted)

    def _next_epoch(self) -> int:
        self._epoch_counter = (self._epoch_counter + 1) & 0xFFFF
        self.epoch = (self.boot_nonce << 16) | self._epoch_counter
        return self.epoch

    def _reset_trace(self) -> None:
        self._queue.clear()
        self.emitted = 0
        self.dropped = 0
        self.aborted = 0
        self.drained = 0
        self._seq = 0

    def reload(self, profile, image: bytes = None) -> None:
        """LOAD equivalent: only while disarmed; new epoch, clear trace."""
        if self.armed:
            raise SimulatorError("cannot reload while armed")
        if self._queue:
            raise SimulatorError("cannot reload with undrained trace events")
        self.profile = profile
        self.template = bytearray(profile.image if image is None else image)
        if len(self.template) != profile.register_space:
            raise ValueError("reloaded image has the wrong size")
        self.image = bytearray(self.template)
        self.pointer = profile.pointer_initial % profile.pointer_wrap
        self.rejected_writes = 0
        self.sealed = True
        self._reset_trace()
        self._next_epoch()

    def arm(self) -> None:
        """Arm the target: restore the template, apply the declared initial
        pointer and start a fresh trace epoch."""
        if self.armed:
            raise SimulatorError("already armed")
        self.image = bytearray(self.template)
        self.pointer = self.profile.pointer_initial % self.profile.pointer_wrap
        self.sealed = False
        self._reset_trace()
        self._next_epoch()
        self.armed = True

    def disarm(self) -> None:
        if self.armed:
            self.armed = False
        self.sealed = True

    def expire(self) -> bool:
        """Lease-expiry equivalent. Returns True if a lease was active."""
        if not self.armed:
            return False
        self.armed = False
        self.sealed = True
        return True

    def note_abort(self) -> None:
        """Fault injection used by the fake: a segment was cut by disarm."""
        if self.armed:
            self.aborted += 1

    def queue_empty(self) -> bool:
        return not self._queue

    # -- bus ------------------------------------------------------------
    def write(self, data: bytes) -> int:
        """One write segment: byte 0 is the pointer, later bytes are data.
        Returns the number of accepted data bytes (rejects are counted)."""
        if not self.armed:
            raise NotArmed("target is not armed")
        data = bytes(data)
        if not data:
            raise ValueError("empty write: at least the pointer byte is required")
        start = data[0] % self.profile.pointer_wrap
        self.pointer = start
        accepted = 0
        for value in data[1:]:
            if self.pointer in self.profile.writable:
                self.image[self.pointer] = value
                accepted += 1
            else:
                self.rejected_writes += 1
            self.pointer = (self.pointer + 1) % self.profile.pointer_wrap
        self._record("w", start, len(data) - 1, bytes(data[1:]))
        return accepted

    def read(self, n: int) -> bytes:
        """One read segment: n bytes, wrapping per byte at pointer_wrap.

        This mirrors core.c exactly: the slave must supply one byte per
        RD_REQ, so a read that reaches the modulus wraps to the start of the
        bounded window instead of running off the end of the image. With
        auto_increment=false the same register is repeated for all n bytes.
        """
        if not self.armed:
            raise NotArmed("target is not armed")
        if not 1 <= n <= self.profile.register_space:
            raise ValueError(f"read length must be 1..{self.profile.register_space}, got {n}")
        start = self.pointer
        out = bytearray()
        for _ in range(n):
            out.append(self.image[self.pointer])
            if self.profile.auto_increment:
                self.pointer = (self.pointer + 1) % self.profile.pointer_wrap
        data = bytes(out)
        self._record("r", start, len(data), data)
        return data

    # -- trace ----------------------------------------------------------
    def _record(self, op: str, ptr: int, length: int, data: bytes) -> None:
        self._tick += 1
        if len(self._queue) >= self.queue_limit:
            self.dropped += 1
            return
        event = Event(
            epoch=self.epoch,
            seq=self._seq,
            op=op,
            ptr=ptr,
            length=length,
            data=data[:TRACE_DATA_MAX],
            truncated=length > TRACE_DATA_MAX,
            tick=self._tick,
        )
        self._seq += 1
        self.emitted += 1
        self._queue.append(event)

    def drain(self, limit: int = DEFAULT_QUEUE_LIMIT) -> List[Event]:
        if limit < 1:
            raise ValueError("drain limit must be >= 1")
        out = self._queue[:limit]
        del self._queue[:limit]
        self.drained += len(out)
        return out

    def assert_complete(self) -> None:
        if self.dropped:
            raise TraceOverflow(
                f"trace overflow: {self.dropped} event(s) dropped; evidence incomplete")
        if self.aborted:
            raise SimulatorError(
                f"{self.aborted} transfer(s) aborted; evidence incomplete")
        if not self.complete_candidate:
            raise SimulatorError("trace is not complete")

    def digest(self) -> str:
        """Deterministic fingerprint of state and trace counters (no wall clock)."""
        blob = json.dumps({
            "profile": self.profile.digest,
            "boot_nonce": self.boot_nonce,
            "epoch": self.epoch,
            "image": bytes(self.image).hex(),
            "template": bytes(self.template).hex(),
            "pointer": self.pointer,
            "emitted": self.emitted,
            "dropped": self.dropped,
            "aborted": self.aborted,
            "drained": self.drained,
            "rejected_writes": self.rejected_writes,
            "armed": self.armed,
            "sealed": self.sealed,
        }, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(blob).hexdigest()
