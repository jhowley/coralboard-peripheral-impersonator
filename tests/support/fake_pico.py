"""SIMULATED TRANSPORT — NO HARDWARE.

This fake speaks the same line protocol as the Pico firmware, on top of the
:class:`coralbench.simulator.SimulatedTarget`, so the host controller code
(paths, epochs, streaming drains, refusals, poison behaviour) can be exercised
without a board.

It is a *test double*, not a device model: expiry is polled by the test clock
(`poll()` / the next line activity), not a hardware alarm, so it cannot prove
anything about firmware timing. Firmware behaviour is evidenced by the compiled
(not executed) firmware and the host-compiled core harness in
`firmware/host_test`.
"""

from __future__ import annotations

import dataclasses
import zlib

from coralbench.simulator import SimulatedTarget

FW_VERSION = "0.2.0"
DEFAULT_IMAGE = bytes(256)
TRACE_DATA_MAX = 8
DRAIN_MAX = 64


class FakePicoTransport:
    def __init__(self, base_profile, *, queue_limit=256, clock=None,
                 lease_ceiling_ms=10000, boot_nonce=0xA5A5):
        self.base_profile = base_profile
        self.queue_limit = queue_limit
        self.clock = clock if clock is not None else _Clock()
        self.lease_ceiling_ms = lease_ceiling_ms
        self.boot_nonce = boot_nonce
        self.target = None
        self.profile = None
        self.expiry = None
        self.sealed = True
        self.expired = False
        self.closed = False
        self.commands = []
        self._pending = []
        # failure injection
        self.fail_mode = None          # None | "garbage" | "timeout" | "disconnect"
        self.fail_after = 0
        self.reply_count = 0

    # -- transport ------------------------------------------------------
    def send_line(self, line):
        if self.closed:
            raise ConnectionError("fake transport closed")
        self._tick()
        self.commands.append(line)
        self._handle(line)

    def read_line(self, timeout_s=1.0):
        self._tick()
        if self.fail_mode == "timeout":
            return None
        if self.fail_mode == "disconnect":
            raise ConnectionError("fake cable pulled")
        if self.fail_mode == "garbage" and self.reply_count >= self.fail_after:
            self.reply_count += 1
            return "GARBAGE !!! not a reply"
        if not self._pending:
            return None
        self.reply_count += 1
        return self._pending.pop(0)

    def close(self):
        self.closed = True

    def pending(self):
        return list(self._pending)

    # -- device ---------------------------------------------------------
    def poll(self):
        """Model the hardware alarm firing without any host command."""
        self._tick()

    def _tick(self):
        if self.expiry is not None and self.clock() >= self.expiry:
            if self.target is not None and self.target.armed:
                self.target.expire()
            self.sealed = True
            self.expired = True
            self.expiry = None

    def _emit(self, line):
        self._pending.append(line)

    def _handle(self, line):
        try:
            self._dispatch(line.strip())
        except (IndexError, ValueError, KeyError) as exc:
            self._emit(f"ERR BAD_COMMAND {exc}")

    def _counts(self):
        t = self.target
        return (t.emitted if t else 0, t.dropped if t else 0,
                t.aborted if t else 0, t.drained if t else 0)

    def _epoch(self):
        return self.target.epoch if self.target else (self.boot_nonce << 16)

    def _cfg(self):
        return self.target.cfg if self.target else 0

    def _ensure_target(self):
        """Create the single persistent device model on first LOAD."""
        if self.target is None:
            self.target = SimulatedTarget(self.base_profile,
                                          queue_limit=self.queue_limit,
                                          boot_nonce=self.boot_nonce)

    def _dispatch(self, line):
        parts = line.split(" ")
        verb = parts[0]
        kv = {}
        for tok in parts[1:]:
            if "=" in tok:
                k, v = tok.split("=", 1)
                kv[k] = v

        if verb == "PING":
            self._emit(f"OK PING m1-i2c fw={FW_VERSION}")

        elif verb == "STATUS":
            emitted, dropped, aborted, drained = self._counts()
            t = self.target
            armed = 1 if (t and t.armed) else 0
            left = 0
            if armed and self.expiry is not None:
                left = max(0, int(round((self.expiry - self.clock()) * 1000)))
            self._emit(
                "OK STATUS armed={} sealed={} loaded={} expired={} addr=0x48 "
                "profile={} crc={} cfg={} epoch={} boot={:04x} lease_left_ms={} "
                "emitted={} dropped={} aborted={} drained={}".format(
                    armed, 1 if self.sealed else 0, 1 if self.profile else 0,
                    1 if self.expired else 0,
                    self.profile.profile_id if self.profile else "none",
                    f"{zlib.crc32(self.profile.image) & 0xffffffff:08x}" if self.profile else "00000000",
                    f"{self._cfg():08x}", f"{self._epoch():08x}", self.boot_nonce,
                    left, emitted, dropped, aborted, drained))

        elif verb == "LOAD":
            if self.target is not None and self.target.armed:
                self._emit("ERR ARM_ACTIVE")
                return
            if self.target is not None and not self.target.queue_empty():
                self._emit("ERR TRACE_PENDING")
                return
            flags = int(kv["flags"], 16)
            image = bytes.fromhex(kv["image"])
            if len(image) != 256:
                self._emit("ERR BAD_IMAGE")
                return
            crc = int(kv["crc"], 16)
            if crc != (zlib.crc32(image) & 0xffffffff):
                self._emit("ERR CRC")
                return
            wmask = bytes.fromhex(kv["wmask"])
            if len(wmask) != 32:
                self._emit("ERR BAD_IMAGE")
                return
            wrap = int(kv["wrap"])
            initial = int(kv["init"])
            if not 1 <= wrap <= 256 or not 0 <= initial < wrap:
                self._emit("ERR BAD_VALUE")
                return
            writable = frozenset(
                reg for reg in range(256) if wmask[reg >> 3] >> (reg & 7) & 1)
            new_profile = dataclasses.replace(
                self.base_profile, image=image, writable=writable,
                auto_increment=bool(flags & 0x01),
                pointer_wrap=wrap, pointer_initial=initial,
                profile_id=kv.get("tag", "loaded"))
            self._ensure_target()
            self.target.reload(new_profile)
            self.profile = new_profile
            self.sealed = True
            self.expired = False
            self.expiry = None
            self._emit(f"OK LOAD crc={crc:08x} len=256 flags={flags:02x} "
                       f"wrap={wrap} init={initial} cfg={self._cfg():08x} "
                       f"epoch={self._epoch():08x}")

        elif verb == "ARM":
            lease = int(kv["lease_ms"])
            if self.target is None or self.profile is None:
                self._emit("ERR NOT_LOADED")
                return
            if self.target.armed:
                self._emit("ERR ARM_ACTIVE")
                return
            if lease < 1 or lease > self.lease_ceiling_ms:
                self._emit("ERR LEASE_TOO_LONG")
                return
            self.target.arm()
            self.sealed = False
            self.expired = False
            self.expiry = self.clock() + lease / 1000.0
            self._emit(f"OK ARM addr=0x48 lease_ms={lease} "
                       f"cfg={self._cfg():08x} epoch={self._epoch():08x}")

        elif verb == "DISARM":
            if self.target is not None and self.target.armed:
                self.target.disarm()
            self.sealed = True
            self.expiry = None
            emitted, dropped, aborted, drained = self._counts()
            self._emit(f"OK DISARM sealed=1 emitted={emitted} dropped={dropped} "
                       f"aborted={aborted} drained={drained} "
                       f"epoch={self._epoch():08x}")

        elif verb == "TRACE":
            sub = parts[1]
            if sub == "DRAIN":
                n = int(kv["n"])
                if not 1 <= n <= DRAIN_MAX:
                    self._emit("ERR BAD_VALUE")
                    return
                events = self.target.drain(n) if self.target else []
                for ev in events:
                    self._emit(f"T epoch={ev.epoch:08x} seq={ev.seq} op={ev.op} "
                               f"ptr={ev.ptr} len={ev.length} data={ev.data.hex()} "
                               f"trunc={1 if ev.truncated else 0}")
                emitted, dropped, aborted, drained = self._counts()
                self._emit(f"OK DRAIN n={len(events)} emitted={emitted} "
                           f"dropped={dropped} aborted={aborted} "
                           f"sealed={1 if self.sealed else 0} "
                           f"epoch={self._epoch():08x} drained={drained}")
            elif sub == "END":
                emitted, dropped, aborted, drained = self._counts()
                complete = 1 if (self.target is not None
                                 and self.target.complete_candidate
                                 and self.sealed) else 0
                self._emit(f"OK TRACEEND emitted={emitted} dropped={dropped} "
                           f"aborted={aborted} sealed={1 if self.sealed else 0} "
                           f"complete={complete} epoch={self._epoch():08x} "
                           f"cfg={self._cfg():08x} drained={drained}")
            else:
                self._emit("ERR BAD_COMMAND")
        else:
            self._emit("ERR BAD_COMMAND")


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
