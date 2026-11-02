"""Byte-level JSONL evidence: sessions (replayable) and raw campaigns.

A *session* is one record per logical transaction, bytes as lowercase hex, so a
diff of two sessions is a byte-level diff. A footer carries a sha256 over the
canonical transaction lines plus an explicit completeness verdict; a tampered,
truncated or trace-incomplete file is never reported as replayable evidence.

A *campaign* is the raw structured trace captured from a device: one record per
bus segment, bound to a profile, configuration CRC, epoch and validated counts.
It is deliberately a different schema: raw segments are not silently presented
as replayable transactions.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

SCHEMA = "coralboard-bench/evidence/v1"
CAMPAIGN_SCHEMA = "coralboard-bench/campaign/v1"
SOURCE_SIMULATION = "simulation"
SOURCE_HARDWARE = "hardware"
SIMULATION_LABEL = "SIMULATED - no hardware involved"
HARDWARE_LABEL = "HARDWARE SESSION - bytes as received from the board"
CAMPAIGN_SIM_LABEL = "SIMULATED CAMPAIGN - no hardware involved"
CAMPAIGN_HW_LABEL = "HARDWARE CAMPAIGN - raw target-side segments, not a wire sniffer"

_HEX_RE = re.compile(r"^[0-9a-f]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _int_meta(meta: dict, key: str, line_no: int = 0) -> Optional[int]:
    """Return an optional integer meta field, rejecting bools/strings."""
    if key not in meta:
        return None
    value = meta[key]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise EvidenceError(f"meta.{key} must be a non-negative integer", line_no)
    return value


def _require_complete_invariants(meta: dict, count: int, *, kind: str,
                                 line_no: int = 0) -> None:
    """Re-derive completeness from metadata; never trust the footer alone."""
    if count < 1:
        raise EvidenceError(
            f"complete {kind} with zero observations is not evidence", line_no)
    dropped = _int_meta(meta, "trace_dropped" if kind == "session" else "dropped",
                        line_no) or 0
    aborted = _int_meta(meta, "trace_aborted" if kind == "session" else "aborted",
                        line_no) or 0
    truncated = bool(meta.get("trace_truncated", False)) if kind == "session" else False
    if dropped or aborted or truncated:
        raise EvidenceError(
            f"complete {kind} reports dropped={dropped} aborted={aborted} "
            f"truncated={int(truncated)}", line_no)

    if kind == "campaign":
        for key in ("emitted", "dropped", "aborted", "drained"):
            if key not in meta or not isinstance(meta[key], int) or isinstance(meta[key], bool):
                raise EvidenceError(
                    f"campaign meta.{key} is required and must be an integer", line_no)
        emitted = int(meta["emitted"])
        drained = int(meta["drained"])
        if emitted != count:
            raise EvidenceError(
                f"complete campaign emitted={emitted} but {count} events present",
                line_no)
        if emitted < 1:
            raise EvidenceError("complete campaign emitted 0 events", line_no)
        if drained != emitted:
            raise EvidenceError(
                f"complete campaign drained={drained} but emitted={emitted}", line_no)
    else:
        emitted = _int_meta(meta, "trace_emitted", line_no)
        drained = _int_meta(meta, "trace_drained", line_no)
        if emitted is not None and emitted < 1:
            raise EvidenceError("complete session emitted 0 segments", line_no)
        if emitted is not None and drained is not None:
            if drained > emitted:
                raise EvidenceError(
                    f"session drained={drained} exceeds emitted={emitted}", line_no)
            if drained != emitted:
                raise EvidenceError(
                    f"complete session drained={drained} but emitted={emitted}",
                    line_no)


class EvidenceError(ValueError):
    def __init__(self, message, line_no: Optional[int] = None):
        self.line_no = line_no
        super().__init__(f"line {line_no}: {message}" if line_no else message)


@dataclass(frozen=True)
class Transaction:
    seq: int
    addr: int
    w: bytes
    r: Optional[bytes]

    def as_dict(self) -> dict:
        return {"type": "tx", "seq": self.seq, "addr": self.addr,
                "w": self.w.hex(), "r": None if self.r is None else self.r.hex()}


@dataclass
class Session:
    meta: Dict[str, object] = field(default_factory=dict)
    transactions: List[Transaction] = field(default_factory=list)
    raw_tx_digest: str = ""
    source: str = ""
    label: str = ""
    device: Optional[str] = None
    complete: bool = False
    incomplete_reason: Optional[str] = None


@dataclass
class Campaign:
    meta: Dict[str, object] = field(default_factory=dict)
    events: List[dict] = field(default_factory=list)
    raw_digest: str = ""
    source: str = ""
    label: str = ""
    device: Optional[str] = None
    complete: bool = False
    incomplete_reason: Optional[str] = None


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def tx_digest(transactions) -> str:
    h = hashlib.sha256()
    for tx in transactions:
        h.update(_canonical(tx.as_dict()).encode() + b"\n")
    return h.hexdigest()


def campaign_digest(events) -> str:
    h = hashlib.sha256()
    for ev in events:
        obj = ev if isinstance(ev, dict) else ev.as_dict()
        h.update(_canonical(obj).encode() + b"\n")
    return h.hexdigest()


def _ascii(value: str, field_name: str, line_no: int) -> bytes:
    if not isinstance(value, str) or not _HEX_RE.match(value):
        raise EvidenceError(f"{field_name} must be lowercase hex, got {value!r}", line_no)
    return bytes.fromhex(value)


def write_session(path, meta: dict, transactions, *,
                  source: str = SOURCE_SIMULATION,
                  device: Optional[str] = None,
                  created_utc: Optional[str] = None,
                  notes: Optional[List[str]] = None,
                  complete: Optional[bool] = None,
                  incomplete_reason: Optional[str] = None) -> dict:
    txs = list(transactions)
    if not txs:
        raise EvidenceError("empty session: refusing to write evidence with no transactions")
    if source not in (SOURCE_SIMULATION, SOURCE_HARDWARE):
        raise EvidenceError(f"unknown evidence source {source!r}")
    if source == SOURCE_HARDWARE:
        if not isinstance(device, str) or not device.strip():
            raise EvidenceError("hardware evidence requires an explicit device path")

    # Never let a caller label a lossy trace complete.
    trace_dropped = int(meta.get("trace_dropped", 0) or 0)
    trace_truncated = bool(meta.get("trace_truncated", False))
    trace_aborted = int(meta.get("trace_aborted", 0) or 0)
    if complete is None:
        # Derive the verdict from the trace metadata. A caller may still pass
        # complete=False explicitly for a loss that is not visible here.
        complete = not (trace_dropped or trace_truncated or trace_aborted)
        if not complete and not incomplete_reason:
            incomplete_reason = "trace metadata reports dropped/truncated/aborted data"
    if complete and (trace_dropped or trace_truncated or trace_aborted):
        raise EvidenceError(
            "refusing complete=True for a session with dropped/truncated/aborted trace data")
    if not complete and not incomplete_reason:
        raise EvidenceError("incomplete evidence requires an explicit reason")
    if complete:
        _require_complete_invariants(meta, len(txs), kind="session")
        # A write-only session has no recorded read byte to compare. It may only
        # be labelled complete when a final-image digest gives replay something
        # meaningful to verify.
        if not any(tx.r for tx in txs):
            image = meta.get("final_image_sha256")
            if not isinstance(image, str) or not _SHA256_RE.match(image):
                raise EvidenceError(
                    "complete write-only session requires final_image_sha256 "
                    "(no read bytes exist to verify)")
    image_meta = meta.get("final_image_sha256")
    if image_meta is not None and (not isinstance(image_meta, str)
                                   or not _SHA256_RE.match(image_meta)):
        raise EvidenceError("final_image_sha256 must be 64 lowercase hex characters")

    digest = tx_digest(txs)
    header = {
        "type": "header",
        "schema": SCHEMA,
        "source": source,
        "label": SIMULATION_LABEL if source == SOURCE_SIMULATION else HARDWARE_LABEL,
        "device": device,
        "created_utc": created_utc or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "meta": dict(meta),
        "notes": list(notes or []),
    }
    footer = {
        "type": "footer",
        "schema": SCHEMA,
        "transactions": len(txs),
        "tx_sha256": digest,
        "complete": bool(complete),
    }
    if not complete:
        footer["incomplete_reason"] = incomplete_reason

    lines = [_canonical(header)]
    lines.extend(_canonical(tx.as_dict()) for tx in txs)
    lines.append(_canonical(footer))
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return {"path": str(path), "transactions": len(txs), "digest": digest,
            "source": source, "label": header["label"], "complete": bool(complete)}


def read_session(path) -> Session:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        raise EvidenceError(f"{path}: {exc.strerror or exc}") from exc

    records = []
    for line_no, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvidenceError(f"invalid JSON ({exc.msg})", line_no) from exc
        if not isinstance(obj, dict):
            raise EvidenceError("record is not a JSON object", line_no)
        records.append((line_no, obj))

    if not records:
        raise EvidenceError(f"{path}: empty evidence file")
    first_line, header = records[0]
    if header.get("type") != "header":
        raise EvidenceError("first record must be a header", first_line)
    if header.get("schema") != SCHEMA:
        raise EvidenceError(f"unsupported schema {header.get('schema')!r}", first_line)
    meta = header.get("meta")
    if not isinstance(meta, dict):
        raise EvidenceError("header.meta must be an object", first_line)

    last_line, footer = records[-1]
    if footer.get("type") != "footer":
        raise EvidenceError("last record must be a footer (truncated evidence?)", last_line)
    if not isinstance(footer.get("complete"), bool):
        raise EvidenceError("footer.complete must be a boolean", last_line)
    if not footer["complete"]:
        reason = footer.get("incomplete_reason")
        if not isinstance(reason, str) or not reason.strip():
            raise EvidenceError(
                "incomplete footer requires a non-empty incomplete_reason", last_line)
    if footer["complete"]:
        if int(meta.get("trace_dropped", 0) or 0) or bool(meta.get("trace_truncated", False)) \
                or int(meta.get("trace_aborted", 0) or 0):
            raise EvidenceError(
                "footer claims complete but header reports lost/aborted trace data", last_line)

    transactions: List[Transaction] = []
    for line_no, obj in records[1:-1]:
        if obj.get("type") != "tx":
            raise EvidenceError(f"unexpected record type {obj.get('type')!r}", line_no)
        for key in ("seq", "addr", "w", "r"):
            if key not in obj:
                raise EvidenceError(f"tx record missing '{key}'", line_no)
        if not isinstance(obj["seq"], int) or obj["seq"] != len(transactions):
            raise EvidenceError(
                f"tx seq {obj['seq']!r} is not the expected {len(transactions)}", line_no)
        if not isinstance(obj["addr"], int) or not 0 <= obj["addr"] <= 0x7F:
            raise EvidenceError(f"addr {obj['addr']!r} is not a 7-bit address", line_no)
        w = _ascii(obj["w"], "w", line_no)
        r = None if obj["r"] is None else _ascii(obj["r"], "r", line_no)
        if len(w) > 256 or (r is not None and len(r) > 256):
            raise EvidenceError("transaction byte string exceeds 256 bytes", line_no)
        transactions.append(Transaction(obj["seq"], obj["addr"], w, r))

    computed = tx_digest(transactions)
    if footer.get("tx_sha256") != computed:
        raise EvidenceError(
            f"digest mismatch: footer {footer.get('tx_sha256')!r} != computed {computed!r}")
    if footer.get("transactions") != len(transactions):
        raise EvidenceError(
            f"count mismatch: footer {footer.get('transactions')!r} != {len(transactions)}")

    if footer["complete"]:
        # A digest-valid footer is not enough: re-derive the invariants the
        # writer promises, so a hand-made "complete" file cannot certify an
        # empty or lossy trace.
        _require_complete_invariants(meta, len(transactions), kind="session",
                                     line_no=last_line)
        if not any(tx.r for tx in transactions):
            image = meta.get("final_image_sha256")
            if not isinstance(image, str) or not _SHA256_RE.match(image):
                raise EvidenceError(
                    "complete write-only session has no read bytes and no "
                    "final_image_sha256 verdict", last_line)
    image_meta = meta.get("final_image_sha256")
    if image_meta is not None and (not isinstance(image_meta, str)
                                   or not _SHA256_RE.match(image_meta)):
        raise EvidenceError("final_image_sha256 must be 64 lowercase hex characters",
                            last_line)

    return Session(meta=meta, transactions=transactions, raw_tx_digest=computed,
                   source=header.get("source", ""), label=header.get("label", ""),
                   device=header.get("device"), complete=footer["complete"],
                   incomplete_reason=footer.get("incomplete_reason"))


def _validate_campaign_event(obj, index: int, line_no: int = 0) -> dict:
    """Structural validation shared by the campaign writer and reader."""
    if not isinstance(obj, dict):
        raise EvidenceError(f"campaign event {index} is not an object", line_no)
    if obj.get("type") != "trace":
        raise EvidenceError(f"campaign event {index} type must be 'trace'", line_no)
    required = ("seq", "epoch", "op", "ptr", "len", "data", "trunc")
    for key in required:
        if key not in obj:
            raise EvidenceError(f"campaign event {index} missing '{key}'", line_no)
    for key in ("seq", "epoch", "ptr", "len"):
        value = obj[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise EvidenceError(
                f"campaign event {index} '{key}' must be a non-negative integer",
                line_no)
    if obj["seq"] != index:
        raise EvidenceError(
            f"campaign event {index} has seq {obj['seq']!r} (not contiguous)", line_no)
    if obj["op"] not in ("r", "w"):
        raise EvidenceError(f"campaign event {index} op must be r or w", line_no)
    if not 0 <= obj["ptr"] <= 255:
        raise EvidenceError(f"campaign event {index} ptr outside 0..255", line_no)
    if not 0 <= obj["len"] <= 256:
        raise EvidenceError(f"campaign event {index} len outside 0..256", line_no)
    data = obj["data"]
    if not isinstance(data, str) or not _HEX_RE.match(data):
        raise EvidenceError(
            f"campaign event {index} data must be lowercase hex", line_no)
    expected = min(int(obj["len"]), 8)
    if len(data) != expected * 2:
        raise EvidenceError(
            f"campaign event {index} data has {len(data)//2} bytes, "
            f"expected {expected} for len={obj['len']}", line_no)
    trunc = obj["trunc"]
    if isinstance(trunc, bool):
        trunc = int(trunc)
    if trunc not in (0, 1):
        raise EvidenceError(f"campaign event {index} trunc must be 0 or 1", line_no)
    if (int(obj["len"]) > 8) != bool(trunc):
        raise EvidenceError(
            f"campaign event {index} trunc={trunc} disagrees with len={obj['len']}",
            line_no)
    return obj


def write_campaign(path, meta: dict, events, *,
                   source: str = SOURCE_HARDWARE,
                   device: Optional[str] = None,
                   created_utc: Optional[str] = None,
                   notes: Optional[List[str]] = None,
                   complete: bool = False,
                   incomplete_reason: Optional[str] = None) -> dict:
    """Write raw structured campaign evidence (segments, not transactions).

    `meta` must carry profile_id/profile_digest/config_crc/epoch/emitted/
    dropped/aborted/drained. A complete campaign must have contiguous sequences,
    matching epoch, no truncation and dropped==aborted==0.
    """
    evs = [ev if isinstance(ev, dict) else ev.as_dict() for ev in events]
    if source not in (SOURCE_SIMULATION, SOURCE_HARDWARE):
        raise EvidenceError(f"unknown evidence source {source!r}")
    if source == SOURCE_HARDWARE:
        if not isinstance(device, str) or not device.strip():
            raise EvidenceError("hardware campaign requires an explicit device path")
    for key in ("profile_id", "profile_digest", "config_crc", "epoch",
                "emitted", "dropped", "aborted", "drained"):
        if key not in meta:
            raise EvidenceError(f"campaign meta missing '{key}'")
    for key in ("config_crc", "epoch", "emitted", "dropped", "aborted", "drained"):
        if not isinstance(meta[key], int) or isinstance(meta[key], bool) \
                or meta[key] < 0:
            raise EvidenceError(f"campaign meta.{key} must be a non-negative integer")

    for i, ev in enumerate(evs):
        _validate_campaign_event(ev, i)

    emitted = int(meta["emitted"])
    dropped = int(meta["dropped"])
    aborted = int(meta["aborted"])
    drained = int(meta["drained"])
    epoch = int(meta["epoch"])
    if complete:
        if emitted != len(evs):
            raise EvidenceError(
                f"complete campaign emitted={emitted} but {len(evs)} events supplied")
        if drained != emitted:
            raise EvidenceError(
                f"complete campaign drained={drained} but emitted={emitted}")
        if dropped or aborted:
            raise EvidenceError("complete campaign cannot contain dropped/aborted data")
        if not evs:
            raise EvidenceError("complete campaign requires at least one observation")
        for i, ev in enumerate(evs):
            if ev.get("epoch") != epoch:
                raise EvidenceError(f"campaign event {i} has a stale epoch")
            if ev.get("trunc"):
                raise EvidenceError(f"campaign event {i} is truncated")
    elif not incomplete_reason:
        raise EvidenceError("incomplete campaign requires an explicit reason")

    digest = campaign_digest(evs)
    header = {
        "type": "header",
        "schema": CAMPAIGN_SCHEMA,
        "source": source,
        "label": CAMPAIGN_SIM_LABEL if source == SOURCE_SIMULATION else CAMPAIGN_HW_LABEL,
        "device": device,
        "created_utc": created_utc or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "meta": dict(meta),
        "notes": list(notes or []),
    }
    footer = {
        "type": "footer",
        "schema": CAMPAIGN_SCHEMA,
        "events": len(evs),
        "events_sha256": digest,
        "complete": bool(complete),
    }
    if not complete:
        footer["incomplete_reason"] = incomplete_reason
    lines = [_canonical(header)]
    lines.extend(_canonical(ev) for ev in evs)
    lines.append(_canonical(footer))
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return {"path": str(path), "events": len(evs), "digest": digest,
            "source": source, "label": header["label"], "complete": bool(complete)}


def read_campaign(path) -> Campaign:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        raise EvidenceError(f"{path}: {exc.strerror or exc}") from exc
    records = []
    for line_no, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvidenceError(f"invalid JSON ({exc.msg})", line_no) from exc
        if not isinstance(obj, dict):
            raise EvidenceError("record is not a JSON object", line_no)
        records.append((line_no, obj))
    if not records:
        raise EvidenceError(f"{path}: empty campaign file")
    first_line, header = records[0]
    if header.get("type") != "header" or header.get("schema") != CAMPAIGN_SCHEMA:
        raise EvidenceError("first record is not a campaign header", first_line)
    meta = header.get("meta")
    if not isinstance(meta, dict):
        raise EvidenceError("campaign header.meta must be an object", first_line)
    for key in ("profile_id", "profile_digest", "config_crc", "epoch",
                "emitted", "dropped", "aborted", "drained"):
        if key not in meta:
            raise EvidenceError(f"campaign meta missing '{key}'", first_line)
    last_line, footer = records[-1]
    if footer.get("type") != "footer" or footer.get("schema") != CAMPAIGN_SCHEMA:
        raise EvidenceError("last record is not a campaign footer", last_line)
    if not isinstance(footer.get("complete"), bool):
        raise EvidenceError("campaign footer.complete must be a boolean", last_line)
    if not footer["complete"]:
        reason = footer.get("incomplete_reason")
        if not isinstance(reason, str) or not reason.strip():
            raise EvidenceError(
                "incomplete campaign footer requires a non-empty incomplete_reason",
                last_line)
    events = []
    for index, (line_no, obj) in enumerate(records[1:-1]):
        events.append(_validate_campaign_event(obj, index, line_no))
    if footer.get("events_sha256") != campaign_digest(events):
        raise EvidenceError("campaign digest mismatch", last_line)
    if footer.get("events") != len(events):
        raise EvidenceError("campaign count mismatch", last_line)
    if footer["complete"]:
        # Re-derive, never trust: dropped/aborted/trunc, emitted/drained,
        # contiguity and one epoch for the whole campaign.
        _require_complete_invariants(meta, len(events), kind="campaign",
                                     line_no=last_line)
        epoch = int(meta["epoch"])
        for index, ev in enumerate(events):
            if ev["epoch"] != epoch:
                raise EvidenceError(
                    f"campaign event {index} epoch {ev['epoch']:08x} != header "
                    f"{epoch:08x}", last_line)
            if ev["trunc"]:
                raise EvidenceError(
                    f"campaign event {index} is truncated; byte trace is not exact",
                    last_line)
    return Campaign(meta=meta, events=events,
                    raw_digest=footer.get("events_sha256", ""),
                    source=header.get("source", ""), label=header.get("label", ""),
                    device=header.get("device"),
                    complete=footer["complete"],
                    incomplete_reason=footer.get("incomplete_reason"))
