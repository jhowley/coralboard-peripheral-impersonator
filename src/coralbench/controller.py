"""Host-side controller for the Pico USB CDC command protocol.

Qualification is deliberately stricter than "the device said OK": a campaign
must carry one epoch, one configuration hash, a contiguous sequence range
0..emitted-1, no drops, no aborts, no truncated events and at least one
observation. A standalone drain advances the device cursor, so it cannot later
be followed by a "complete" collect of the tail.
"""

from __future__ import annotations

import time
import zlib
from dataclasses import dataclass, field
from typing import List, Optional

from .profile import config_crc, wmask_bytes
from .serial_link import SerialLinkError

LEASE_CEILING_MS = 10000
MAX_LINE = 1024
DRAIN_MAX = 64

_STATUS_KEYS = ("armed", "sealed", "loaded", "expired", "addr", "profile",
                "crc", "cfg", "epoch", "boot", "lease_left_ms", "emitted",
                "dropped", "aborted", "drained")


class ControllerError(RuntimeError):
    pass


class ProtocolError(ControllerError):
    pass


class TraceIncomplete(ControllerError):
    pass


@dataclass(frozen=True)
class DeviceStatus:
    armed: bool
    sealed: bool
    loaded: bool
    expired: bool
    address: int
    profile_tag: str
    image_crc: int
    config_crc: int
    epoch: int
    boot_nonce: int
    lease_left_ms: int
    trace_emitted: int
    trace_dropped: int
    trace_aborted: int
    trace_drained: int
    raw: str = ""

    @property
    def complete_candidate(self) -> bool:
        return (self.sealed and not self.armed and self.trace_dropped == 0
                and self.trace_aborted == 0 and self.trace_emitted > 0
                and self.trace_drained == self.trace_emitted)


@dataclass(frozen=True)
class TraceEvent:
    epoch: int
    seq: int
    op: str
    ptr: int
    length: int
    data: bytes
    truncated: bool


@dataclass
class CampaignEvidence:
    profile_id: str
    profile_digest: str
    config_crc: int
    epoch: int
    events: List[TraceEvent] = field(default_factory=list)
    status: Optional[DeviceStatus] = None
    drained_rounds: int = 0

    @property
    def complete(self) -> bool:
        return bool(self.status and self.status.complete_candidate
                    and self.status.epoch == self.epoch
                    and self.status.config_crc == self.config_crc
                    and self.events
                    and [e.seq for e in self.events] == list(range(len(self.events)))
                    and all(e.epoch == self.epoch for e in self.events)
                    and not any(e.truncated for e in self.events))


def _tokens(line: str, prefix: str) -> dict:
    if not line.startswith(prefix):
        raise ProtocolError(f"expected reply starting with {prefix!r}, got {line!r}")
    tokens = {}
    for token in line[len(prefix):].split():
        if "=" in token:
            key, value = token.split("=", 1)
            if key in tokens:
                raise ProtocolError(f"duplicate key {key!r} in {line!r}")
            tokens[key] = value
    return tokens


def _int(tokens: dict, key: str, line: str) -> int:
    if key not in tokens:
        raise ProtocolError(f"missing '{key}' in {line!r}")
    try:
        return int(tokens[key], 0)
    except ValueError as exc:
        raise ProtocolError(f"non-numeric {key}={tokens[key]!r} in {line!r}") from exc


def _hexint(tokens: dict, key: str, line: str) -> int:
    """Hex fields are printed without a 0x prefix; accept both forms."""
    if key not in tokens:
        raise ProtocolError(f"missing '{key}' in {line!r}")
    value = tokens[key]
    try:
        return int(value, 16)
    except ValueError as exc:
        raise ProtocolError(f"non-hex {key}={value!r} in {line!r}") from exc


class PicoController:
    def __init__(self, transport, *, lease_ceiling_ms: int = LEASE_CEILING_MS,
                 drain_chunk: int = 16, max_drain_rounds: int = 64,
                 timeout_s: float = 2.0, clock=time.monotonic,
                 sleeper=time.sleep):
        if not 1 <= drain_chunk <= DRAIN_MAX:
            raise ControllerError(f"drain_chunk must be 1..{DRAIN_MAX}")
        if max_drain_rounds < 1:
            raise ControllerError("max_drain_rounds must be >= 1")
        if not 1 <= lease_ceiling_ms <= LEASE_CEILING_MS:
            raise ControllerError(f"lease_ceiling_ms must be 1..{LEASE_CEILING_MS}")
        self.transport = transport
        self.lease_ceiling_ms = lease_ceiling_ms
        self.drain_chunk = drain_chunk
        self.max_drain_rounds = max_drain_rounds
        self.timeout_s = timeout_s
        self.clock = clock
        self.sleeper = sleeper
        self.closed = False
        self.poisoned: Optional[str] = None

    # -- plumbing -------------------------------------------------------
    def _check_usable(self) -> None:
        if self.poisoned:
            raise ControllerError(
                f"transport poisoned after {self.poisoned}; refusing to reuse stale replies")

    def _poison(self, reason: str) -> None:
        self.poisoned = reason

    def _write(self, line: str) -> None:
        self._check_usable()
        writer = getattr(self.transport, "send_line", None) or getattr(
            self.transport, "write_line", None)
        if writer is None:
            raise ControllerError("transport has neither send_line nor write_line")
        try:
            writer(line)
        except (OSError, ConnectionError, SerialLinkError) as exc:
            self._poison(f"disconnect while sending {line!r}")
            raise ControllerError(f"disconnect while sending {line!r}: {exc}") from exc

    def _read(self) -> str:
        self._check_usable()
        reader = getattr(self.transport, "read_line", None)
        if reader is None:
            raise ControllerError("transport has no read_line")
        try:
            line = reader(self.timeout_s)
        except (OSError, ConnectionError, SerialLinkError) as exc:
            self._poison("disconnect while reading a reply")
            raise ControllerError(f"disconnect while reading reply: {exc}") from exc
        if line is None:
            self._poison("reply timeout")
            raise ControllerError(f"timeout after {self.timeout_s:.1f}s waiting for a reply")
        line = line.strip()
        if not line:
            self._poison("empty reply line")
            raise ProtocolError("empty reply line")
        if line.startswith("ERR"):
            raise ProtocolError(f"device refused: {line}")
        return line

    def _cmd(self, line: str) -> str:
        self._write(line)
        return self._read()

    def _parse_status(self, line: str) -> DeviceStatus:
        tokens = _tokens(line, "OK STATUS")
        for key in _STATUS_KEYS:
            if key not in tokens:
                self._poison(f"status missing '{key}'")
                raise ProtocolError(f"status missing '{key}' in {line!r}")
        return DeviceStatus(
            armed=_int(tokens, "armed", line) == 1,
            sealed=_int(tokens, "sealed", line) == 1,
            loaded=_int(tokens, "loaded", line) == 1,
            expired=_int(tokens, "expired", line) == 1,
            address=_int(tokens, "addr", line),
            profile_tag=tokens["profile"],
            image_crc=_hexint(tokens, "crc", line),
            config_crc=_hexint(tokens, "cfg", line),
            epoch=_hexint(tokens, "epoch", line),
            boot_nonce=_hexint(tokens, "boot", line),
            lease_left_ms=_int(tokens, "lease_left_ms", line),
            trace_emitted=_int(tokens, "emitted", line),
            trace_dropped=_int(tokens, "dropped", line),
            trace_aborted=_int(tokens, "aborted", line),
            trace_drained=_int(tokens, "drained", line),
            raw=line,
        )

    def _parse_event(self, line: str) -> TraceEvent:
        tokens = _tokens(line, "T ")
        for key in ("epoch", "seq", "op", "ptr", "len", "data"):
            if key not in tokens:
                self._poison(f"trace event missing '{key}'")
                raise ProtocolError(f"trace event missing '{key}' in {line!r}")
        if tokens["op"] not in ("r", "w"):
            raise ProtocolError(f"trace event op must be r or w, got {tokens['op']!r}")
        data = tokens["data"]
        if len(data) % 2 or any(c not in "0123456789abcdef" for c in data):
            raise ProtocolError(f"trace event data is not lowercase hex: {data!r}")
        return TraceEvent(
            epoch=_hexint(tokens, "epoch", line),
            seq=_int(tokens, "seq", line),
            op=tokens["op"],
            ptr=_int(tokens, "ptr", line),
            length=_int(tokens, "len", line),
            data=bytes.fromhex(data),
            truncated=tokens.get("trunc") == "1",
        )

    # -- commands -------------------------------------------------------
    def status(self) -> DeviceStatus:
        return self._parse_status(self._cmd("STATUS"))

    def load_image(self, profile) -> DeviceStatus:
        image = bytes(profile.image)
        if len(image) != profile.register_space:
            raise ControllerError(
                f"refusing to load: image is {len(image)} bytes, "
                f"register_space is {profile.register_space}")
        crc = zlib.crc32(image) & 0xFFFFFFFF
        flags = 0x01 if profile.auto_increment else 0x00
        line = (f"LOAD crc={crc:08x} flags={flags:02x} wrap={profile.pointer_wrap} "
                f"init={profile.pointer_initial} tag={profile.profile_id} "
                f"wmask={wmask_bytes(profile).hex()} image={image.hex()}")
        reply = self._cmd(line)
        try:
            tokens = _tokens(reply, "OK LOAD")
        except ProtocolError:
            self._poison("malformed LOAD reply")
            raise
        try:
            echoed_crc = int(tokens.get("crc", ""), 16)
        except ValueError as exc:
            self._poison("non-hex LOAD crc")
            raise ProtocolError(f"device echoed a non-hex crc in {reply!r}") from exc
        if echoed_crc != crc:
            raise ProtocolError(f"device echoed crc {tokens.get('crc')!r}, expected {crc:08x}")
        if _int(tokens, "len", reply) != len(image):
            raise ProtocolError(f"device stored {tokens.get('len')!r} bytes, expected {len(image)}")
        if _int(tokens, "wrap", reply) != profile.pointer_wrap:
            raise ProtocolError(f"device applied wrap {tokens.get('wrap')!r}, "
                                f"expected {profile.pointer_wrap}")
        if _int(tokens, "init", reply) != profile.pointer_initial:
            raise ProtocolError(f"device applied init {tokens.get('init')!r}, "
                                f"expected {profile.pointer_initial}")
        expected_cfg = config_crc(profile)
        if _hexint(tokens, "cfg", reply) != expected_cfg:
            raise ProtocolError(
                f"device cfg {tokens.get('cfg')!r} != profile cfg {expected_cfg:08x}")
        st = self.status()
        if st.config_crc != expected_cfg or st.profile_tag != profile.profile_id:
            raise ProtocolError("loaded status does not match the requested profile")
        return st

    def arm(self, lease_ms: int) -> DeviceStatus:
        if not isinstance(lease_ms, int) or isinstance(lease_ms, bool):
            raise ControllerError("lease_ms must be an integer")
        ceiling = min(self.lease_ceiling_ms, LEASE_CEILING_MS)
        if not 1 <= lease_ms <= ceiling:
            raise ControllerError(f"lease {lease_ms}ms outside the allowed 1..{ceiling}ms bound")
        reply = self._cmd(f"ARM lease_ms={lease_ms}")
        tokens = _tokens(reply, "OK ARM")
        if _int(tokens, "lease_ms", reply) != lease_ms:
            raise ProtocolError(f"device applied a different lease: {reply!r}")
        return self.status()

    def disarm(self) -> DeviceStatus:
        reply = self._cmd("DISARM")
        _tokens(reply, "OK DISARM")  # shape check only; STATUS is authoritative
        return self.status()

    def trace_end(self) -> dict:
        reply = self._cmd("TRACE END")
        tokens = _tokens(reply, "OK TRACEEND")
        return {
            "emitted": _int(tokens, "emitted", reply),
            "dropped": _int(tokens, "dropped", reply),
            "aborted": _int(tokens, "aborted", reply),
            "sealed": _int(tokens, "sealed", reply) == 1,
            "complete": _int(tokens, "complete", reply) == 1,
            "epoch": _hexint(tokens, "epoch", reply),
            "cfg": _hexint(tokens, "cfg", reply),
            "drained": _int(tokens, "drained", reply),
        }

    def _drain_round(self):
        self._write(f"TRACE DRAIN n={self.drain_chunk}")
        events: List[TraceEvent] = []
        while True:
            line = self._read()
            if line.startswith("T "):
                events.append(self._parse_event(line))
                continue
            tokens = _tokens(line, "OK DRAIN")
            announced = _int(tokens, "n", line)
            if announced != len(events):
                self._poison("drain count mismatch")
                raise ProtocolError(
                    f"device announced n={announced} but streamed {len(events)} event lines")
            return events, {
                "emitted": _int(tokens, "emitted", line),
                "dropped": _int(tokens, "dropped", line),
                "aborted": _int(tokens, "aborted", line),
                "sealed": _int(tokens, "sealed", line) == 1,
                "epoch": _hexint(tokens, "epoch", line),
                "drained": _int(tokens, "drained", line),
            }

    def drain(self) -> List[TraceEvent]:
        """Stream events, including while armed (streaming observation).

        The device cursor advances. Calling this outside a collect means those
        events can no longer qualify a campaign: collect requires drained==0.
        """
        st = self.status()
        events: List[TraceEvent] = []
        rounds = 0
        while True:
            rounds += 1
            if rounds > self.max_drain_rounds:
                raise ControllerError(
                    f"drain exceeded {self.max_drain_rounds} rounds; device may be stuck")
            batch, info = self._drain_round()
            if info["epoch"] != st.epoch:
                raise TraceIncomplete("epoch changed mid-drain (reload or re-arm)")
            if info["dropped"]:
                raise TraceIncomplete(
                    f"trace overflow: device reports {info['dropped']} dropped event(s)")
            if info["aborted"]:
                raise TraceIncomplete(
                    f"device reports {info['aborted']} aborted transfer(s)")
            if info["drained"] != st.trace_drained + len(events) + len(batch):
                raise TraceIncomplete("drain cursor did not advance by the events received")
            events.extend(batch)
            if not batch:
                return events

    def _bind_profile(self, profile, st: DeviceStatus) -> int:
        if not st.loaded:
            raise ControllerError("device has no register image loaded")
        if st.profile_tag != profile.profile_id:
            raise ControllerError(
                f"loaded profile {st.profile_tag!r} is not the requested {profile.profile_id!r}")
        expected_image_crc = zlib.crc32(profile.image) & 0xFFFFFFFF
        if st.image_crc != expected_image_crc:
            raise ControllerError(
                f"image crc 0x{st.image_crc:08x} != profile crc 0x{expected_image_crc:08x}")
        expected_cfg = config_crc(profile)
        if st.config_crc != expected_cfg:
            raise ControllerError(
                f"device cfg 0x{st.config_crc:08x} != profile cfg 0x{expected_cfg:08x}")
        return expected_cfg

    def _absorb(self, st: DeviceStatus, info: dict, batch, events) -> None:
        if info["epoch"] != st.epoch:
            raise TraceIncomplete("epoch changed mid-collect (reload or re-arm)")
        if info["dropped"]:
            raise TraceIncomplete(f"trace overflow: {info['dropped']} dropped event(s)")
        if info["aborted"]:
            raise TraceIncomplete(f"{info['aborted']} aborted transfer(s)")
        if info["drained"] != st.trace_drained + len(events) + len(batch):
            raise TraceIncomplete("drain cursor did not advance by the events received")

    def _finish(self, profile, st: DeviceStatus, events: List[TraceEvent],
                expected_cfg: int, expect_events: Optional[int]) -> CampaignEvidence:
        end = self.trace_end()
        after = self.status()
        if after.epoch != st.epoch or end["epoch"] != st.epoch:
            raise TraceIncomplete("epoch changed between drain and verdict")
        if after.config_crc != expected_cfg or end["cfg"] != expected_cfg:
            raise TraceIncomplete("configuration changed between drain and verdict")
        if after.armed:
            raise ControllerError("device re-armed during collect")
        if end["dropped"] or end["aborted"]:
            raise TraceIncomplete(
                f"trace incomplete (dropped={end['dropped']}, aborted={end['aborted']})")
        if not end["sealed"]:
            raise TraceIncomplete("device reports trace not sealed; evidence could still grow")
        if not end["complete"]:
            raise TraceIncomplete(
                f"device reports incomplete trace (sealed={end['sealed']}, "
                f"dropped={end['dropped']}, aborted={end['aborted']})")
        emitted = end["emitted"]
        if emitted == 0:
            raise TraceIncomplete("zero-observation campaign is not qualifying evidence")
        if after.trace_emitted != emitted:
            raise TraceIncomplete(
                f"device emitted count changed between drain and verdict "
                f"({after.trace_emitted} != {emitted})")
        if len(events) != emitted:
            raise TraceIncomplete(f"device emitted {emitted} events but {len(events)} arrived")
        if end["drained"] != emitted or after.trace_drained != emitted:
            raise TraceIncomplete(
                f"drain cursor {after.trace_drained} != emitted {emitted}")
        seqs = [e.seq for e in events]
        if seqs != list(range(emitted)):
            if len(set(seqs)) != len(seqs):
                raise TraceIncomplete("duplicate trace sequence number")
            raise TraceIncomplete("trace sequence has a gap (missing event)")
        if any(e.epoch != st.epoch for e in events):
            raise TraceIncomplete("stale trace event from another epoch")
        if any(e.truncated for e in events):
            raise TraceIncomplete("trace event was truncated (>8 bytes); byte trace is not exact")
        if expect_events is not None and len(events) != expect_events:
            raise TraceIncomplete(
                f"expected {expect_events} events, drained {len(events)}")
        return CampaignEvidence(profile_id=profile.profile_id,
                                profile_digest=profile.digest,
                                config_crc=expected_cfg,
                                epoch=st.epoch,
                                events=events,
                                status=after)

    def collect_campaign(self, profile, *, expect_events: Optional[int] = None) -> CampaignEvidence:
        """Collect a sealed, not-yet-drained epoch (the device must be disarmed)."""
        st = self.status()
        if st.armed:
            raise ControllerError("device is armed: use collect_live or let the lease expire")
        expected_cfg = self._bind_profile(profile, st)
        if st.trace_drained != 0:
            raise TraceIncomplete(
                f"trace already partially drained ({st.trace_drained} events); "
                "start a new lease before collecting evidence")
        if st.trace_dropped:
            raise TraceIncomplete(
                f"trace overflow: {st.trace_dropped} dropped event(s); refusing campaign")
        if st.trace_aborted:
            raise TraceIncomplete(
                f"{st.trace_aborted} aborted transfer(s); refusing campaign")

        events: List[TraceEvent] = []
        rounds = 0
        while True:
            rounds += 1
            if rounds > self.max_drain_rounds:
                raise ControllerError(
                    f"drain exceeded {self.max_drain_rounds} rounds; device may be stuck")
            batch, info = self._drain_round()
            self._absorb(st, info, batch, events)
            events.extend(batch)
            if not batch:
                break
        return self._finish(profile, st, events, expected_cfg, expect_events)

    def collect_live(self, profile, *, timeout_s: float = 30.0,
                     max_events: int = 4096, poll_s: float = 0.02,
                     sleeper=None) -> CampaignEvidence:
        """Stream the *same* epoch until the lease seals, then validate.

        This is the primary hardware workflow: issued while `armed=1`, it
        drains in bounded rounds, accumulates the contiguous epoch and only
        reports success once the device itself says sealed/unarmed/no-loss.
        A timeout or event-count budget aborts without writing evidence, so a
        partial trace is never silently promoted to a campaign.
        """
        if not 0 < timeout_s <= 600:
            raise ControllerError("timeout_s must be in (0, 600]")
        if max_events < 1:
            raise ControllerError("max_events must be >= 1")
        if not 0 < poll_s <= 1:
            raise ControllerError("poll_s must be in (0, 1]")
        st = self.status()
        expected_cfg = self._bind_profile(profile, st)
        if not st.armed:
            # Already sealed: the ordinary collect path is stricter about the
            # undrained cursor and avoids an invented streaming claim.
            return self.collect_campaign(profile)
        if st.trace_drained != 0:
            raise TraceIncomplete(
                f"trace already partially drained ({st.trace_drained} events); "
                "a streaming collect must start at seq 0")
        if st.trace_dropped:
            raise TraceIncomplete(
                f"trace overflow: {st.trace_dropped} dropped event(s); refusing campaign")
        if st.trace_aborted:
            raise TraceIncomplete(
                f"{st.trace_aborted} aborted transfer(s); refusing campaign")

        deadline = self.clock() + timeout_s
        max_rounds = min(1_000_000, int(timeout_s / poll_s) + 16)
        sleep = self.sleeper if sleeper is None else sleeper
        events: List[TraceEvent] = []
        rounds = 0

        while True:
            rounds += 1
            if rounds > max_rounds:
                raise TraceIncomplete("streaming collect exceeded its poll-round budget")
            batch, info = self._drain_round()
            self._absorb(st, info, batch, events)
            events.extend(batch)
            if len(events) > max_events:
                raise TraceIncomplete(
                    f"streaming collect exceeded max_events={max_events}; "
                    "the lease produced more observations than budgeted")
            after = self.status()
            if after.epoch != st.epoch:
                raise TraceIncomplete("epoch changed mid-stream (reload or re-arm)")
            if after.trace_dropped:
                raise TraceIncomplete(
                    f"trace overflow during streaming: {after.trace_dropped} dropped")
            if after.trace_aborted:
                raise TraceIncomplete(
                    f"{after.trace_aborted} abort(s) during streaming")
            if not after.armed:
                if not after.sealed:
                    raise TraceIncomplete("device disarmed but trace is not sealed")
                break
            if self.clock() >= deadline:
                raise TraceIncomplete(
                    f"streaming collect timed out after {timeout_s:.1f}s while the "
                    "lease was still armed; no evidence written")
            sleep(poll_s)

        # The lease is sealed: drain the events still sitting in the ring.
        while True:
            rounds += 1
            if rounds > max_rounds:
                raise TraceIncomplete("post-seal drain exceeded its round budget")
            batch, info = self._drain_round()
            self._absorb(st, info, batch, events)
            events.extend(batch)
            if len(events) > max_events:
                raise TraceIncomplete(
                    f"post-seal drain exceeded max_events={max_events}")
            if not batch:
                break
        return self._finish(profile, st, events, expected_cfg, None)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.transport.close()
        except (OSError, ConnectionError) as exc:
            raise ControllerError(f"error closing transport: {exc}") from exc
