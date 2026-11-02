"""Deterministic replay of recorded evidence against a profile.

Replay is the anti-fabrication check: recorded read bytes must be reproduced
byte-for-byte by the simulator running the recorded writes. Any mismatch is
reported with its sequence number, never smoothed over.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import List

from .evidence import tx_digest
from .simulator import SimulatedTarget


class ReplayError(RuntimeError):
    pass


@dataclass
class Mismatch:
    seq: int
    field: str
    expected: str
    actual: str


@dataclass
class ReplayResult:
    ok: bool
    profile_id: str
    transactions: int
    mismatches: List[Mismatch] = field(default_factory=list)
    checks: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if self.ok:
            return "PASS"
        if any(n.startswith(("inconclusive", "unsupported")) for n in self.notes):
            return "INCONCLUSIVE"
        return "FAIL"

    def summary(self) -> str:
        return (f"replay {self.verdict} profile={self.profile_id} "
                f"tx={self.transactions} mismatches={len(self.mismatches)} "
                f"checks={len(self.checks)}")


def replay_session(session, profile) -> ReplayResult:
    result = ReplayResult(ok=True, profile_id=profile.profile_id,
                          transactions=len(session.transactions))

    if not getattr(session, "complete", False):
        result.notes.append("evidence_incomplete")
        if getattr(session, "incomplete_reason", None):
            result.notes.append(f"incomplete_reason: {session.incomplete_reason}")
        result.ok = False
        return result

    header_digest = session.meta.get("profile_digest")
    if header_digest is None:
        result.notes.append("profile_digest_missing")
        result.ok = False
    elif header_digest != profile.digest:
        result.notes.append("profile_digest_mismatch")
        result.ok = False
    else:
        result.checks.append("profile_digest")

    header_profile = session.meta.get("profile_id")
    if header_profile is not None and header_profile != profile.profile_id:
        result.notes.append("profile_id_mismatch")
        result.ok = False

    recorded_digest = session.raw_tx_digest or tx_digest(session.transactions)
    if recorded_digest == tx_digest(session.transactions):
        result.checks.append("tx_digest")
    else:
        result.notes.append("tx_digest_mismatch")
        result.ok = False

    target = SimulatedTarget(profile)
    target.arm()
    try:
        for tx in session.transactions:
            if tx.addr != profile.address:
                result.notes.append(
                    f"addr_mismatch@{tx.seq}: 0x{tx.addr:02x} != 0x{profile.address:02x}")
                result.ok = False
                break  # stop: replaying later reads from a desynchronised image is noise
            if tx.w:
                target.write(tx.w)
            if tx.r is not None:
                actual = target.read(len(tx.r))
                if actual != tx.r:
                    result.mismatches.append(
                        Mismatch(tx.seq, "read", tx.r.hex(), actual.hex()))
    finally:
        target.disarm()

    if result.mismatches:
        result.ok = False
    else:
        result.checks.append("replay_bytes")
    if target.dropped:
        result.notes.append(f"trace_overflow: {target.dropped} dropped")
        result.ok = False

    # A write-only session has no recorded read byte, so the normal replay
    # comparison proves nothing. It is only conclusive if the final image can
    # be checked; otherwise report INCONCLUSIVE rather than a tidy PASS.
    has_reads = any(tx.r for tx in session.transactions)
    expected_image = session.meta.get("final_image_sha256")
    if not has_reads and not expected_image:
        result.notes.append(
            "inconclusive: write-only session has no read bytes and no "
            "final_image_sha256; replay cannot verify target state")
        result.ok = False

    expected_rejected = session.meta.get("rejected_writes")
    if expected_rejected is not None:
        if int(expected_rejected) != target.rejected_writes:
            result.notes.append(
                f"rejected_writes_mismatch: recorded={expected_rejected} "
                f"replayed={target.rejected_writes}")
            result.ok = False
        else:
            result.checks.append("rejected_writes")
    elif target.rejected_writes:
        result.notes.append(f"rejected_writes: {target.rejected_writes}")

    expected_image = session.meta.get("final_image_sha256")
    if expected_image:
        actual_image = hashlib.sha256(bytes(target.image)).hexdigest()
        if actual_image != expected_image:
            result.notes.append("final_image_mismatch")
            result.ok = False
        else:
            result.checks.append("final_image")
            if not has_reads:
                result.notes.append("write_only_verified_by_final_image")
    return result
