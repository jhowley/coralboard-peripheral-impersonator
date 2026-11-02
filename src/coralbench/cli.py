"""coralbench CLI.

Every subcommand is offline by default; `serial` is the only one that opens a
device and it requires an explicit, existing, non-glob path. The simulate verb
labels its output SIMULATED in both stdout and the evidence header.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

from . import MILESTONE, __version__
from . import evidence as ev
from . import profile as prof
from . import replay as rp
from .controller import (ControllerError, LEASE_CEILING_MS, PicoController,
                         ProtocolError, TraceIncomplete)
from .serial_link import PosixSerial, SerialLinkError, check_device_path
from .simulator import SimulatedTarget
from .usb_link import USBLink, USBLinkError, check_serial_number

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REPLAY_FAIL = 3


def _err(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)


def _load_ops(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except OSError as exc:
        raise ValueError(f"{path}: {exc.strerror or exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: {exc.msg} at line {exc.lineno}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("ops"), list):
        raise ValueError(f"{path}: expected an object with an 'ops' list")
    ops = doc["ops"]
    if not ops:
        raise ValueError(f"{path}: 'ops' is empty")
    for i, op in enumerate(ops):
        if not isinstance(op, dict) or ("w" in op) == ("r" in op):
            raise ValueError(f"{path}: op {i} must have exactly one of 'w' or 'r'")
        if "w" in op:
            value = op["w"]
            if not isinstance(value, str) or len(value) % 2 or any(
                    c not in "0123456789abcdef" for c in value):
                raise ValueError(f"{path}: op {i} 'w' must be lowercase even-length hex")
            if not value:
                raise ValueError(f"{path}: op {i} 'w' must not be empty")
        else:
            if not isinstance(op["r"], int) or isinstance(op["r"], bool) or op["r"] < 1:
                raise ValueError(f"{path}: op {i} 'r' must be a positive integer")
    return doc


def cmd_validate(args) -> int:
    p = prof.load_profile(args.profile)
    print(f"OK validate profile={p.profile_id} addr=0x{p.address:02x} "
          f"image_bytes={len(p.image)} lease_max_ms={p.lease_max_ms} digest={p.digest}")
    return EXIT_OK


def cmd_simulate(args) -> int:
    p = prof.load_profile(args.profile)
    doc = _load_ops(args.ops)
    target = SimulatedTarget(p, queue_limit=args.queue_limit)
    target.arm()
    transactions = []
    try:
        for i, op in enumerate(doc["ops"]):
            w = b"" if "w" not in op else bytes.fromhex(op["w"])
            r = None
            if w:
                target.write(w)
            if "r" in op:
                r = target.read(op["r"])
            transactions.append(ev.Transaction(i, p.address, w, r))
    finally:
        target.disarm()

    trace_complete = target.dropped == 0 and target.aborted == 0
    meta = {
        "profile_id": p.profile_id,
        "profile_digest": p.digest,
        "config_crc": prof.config_crc(p),
        "label": args.label or doc.get("label", "simulate"),
        "ops_source": os.path.abspath(args.ops),
        "target_digest": target.digest(),
        "trace_emitted": target.emitted,
        "trace_drained": target.emitted,
        "trace_dropped": target.dropped,
        "trace_aborted": target.aborted,
        "final_image_sha256": hashlib.sha256(bytes(target.image)).hexdigest(),
        "rejected_writes": target.rejected_writes,
    }
    reason = None
    if not trace_complete:
        reason = (f"simulated trace lost events (dropped={target.dropped}, "
                  f"aborted={target.aborted})")
    info = ev.write_session(args.out, meta, transactions, complete=trace_complete,
                            incomplete_reason=reason)
    verdict = "SIMULATED" if trace_complete else "SIMULATED INCOMPLETE"
    print(f"{verdict} ({ev.SIMULATION_LABEL}): wrote {info['transactions']} transactions "
          f"to {args.out} profile={p.profile_id} digest={info['digest'][:16]} "
          f"trace_dropped={target.dropped}")
    print("note: this file contains simulated bytes only; it is not hardware evidence.")
    if not trace_complete:
        print(f"note: evidence is marked incomplete: {reason}")
        return EXIT_ERROR
    return EXIT_OK


def cmd_replay(args) -> int:
    p = prof.load_profile(args.profile)
    session = ev.read_session(args.evidence)
    result = rp.replay_session(session, p)
    print(result.summary())
    for check in result.checks:
        print(f"  check {check}: ok")
    for note in result.notes:
        print(f"  note {note}")
    for mismatch in result.mismatches[:10]:
        print(f"  mismatch seq={mismatch.seq} {mismatch.field} "
              f"recorded={mismatch.expected} replayed={mismatch.actual}")
    if len(result.mismatches) > 10:
        print(f"  ... {len(result.mismatches) - 10} more mismatches")
    return EXIT_OK if result.ok else EXIT_REPLAY_FAIL


class _SerialAdapter:
    """Adapts PosixSerial to the controller's send_line/read_line shape."""

    def __init__(self, link):
        self.link = link

    def send_line(self, line):
        self.link.write_line(line)

    def read_line(self, timeout_s):
        return self.link.read_line(timeout_s)

    def close(self):
        self.link.close()


def cmd_serial(args) -> int:
    # Bound checks come before the device is touched at all.
    if args.command == "arm" and not 1 <= args.lease_ms <= LEASE_CEILING_MS:
        _err(f"lease {args.lease_ms}ms outside the allowed 1..{LEASE_CEILING_MS}ms bound")
        return EXIT_ERROR
    path = check_device_path(args.device)

    link = PosixSerial(path, baud=args.baud, timeout_s=args.timeout)
    link.open()
    try:
        ctl = PicoController(_SerialAdapter(link), timeout_s=args.timeout,
                             lease_ceiling_ms=LEASE_CEILING_MS)
    except Exception:
        link.close()
        raise
    try:
        return _run_device_command(ctl, args, path)
    finally:
        ctl.close()


def cmd_usb(args) -> int:
    """Same verbs over the libusb CDC transport, selected by exact serial."""
    if args.command == "arm" and not 1 <= args.lease_ms <= LEASE_CEILING_MS:
        _err(f"lease {args.lease_ms}ms outside the allowed 1..{LEASE_CEILING_MS}ms bound")
        return EXIT_ERROR
    serial = check_serial_number(args.serial)
    link = USBLink(serial, timeout_s=args.timeout)
    link.open()
    try:
        ctl = PicoController(link, timeout_s=args.timeout,
                             lease_ceiling_ms=LEASE_CEILING_MS)
    except Exception:
        link.close()
        raise
    try:
        return _run_device_command(ctl, args, f"usb:{serial}")
    finally:
        ctl.close()


def _run_device_command(ctl, args, device_label: str) -> int:
    """Shared status/load/arm/disarm/drain/collect execution."""
    if args.command == "status":
        _print_status(ctl.status())
    elif args.command == "load":
        p = prof.load_profile(args.profile)
        _print_status(ctl.load_image(p))
        print(f"loaded profile={p.profile_id} crc=0x{_crc(p):08x}")
    elif args.command == "arm":
        _print_status(ctl.arm(args.lease_ms))
    elif args.command == "disarm":
        _print_status(ctl.disarm())
    elif args.command == "drain":
        for event in ctl.drain():
            print(json.dumps({"epoch": event.epoch, "seq": event.seq,
                              "op": event.op, "ptr": event.ptr,
                              "len": event.length, "data": event.data.hex(),
                              "truncated": event.truncated}, sort_keys=True))
    elif args.command == "collect":
        p = prof.load_profile(args.profile)
        # Primary workflow: issued while armed, streams the same epoch in
        # bounded rounds until the lease seals, then validates and writes.
        camp = ctl.collect_live(
            p,
            timeout_s=args.collect_timeout,
            max_events=args.max_events,
            poll_s=args.poll_ms / 1000.0,
        )
        print(f"campaign complete profile={camp.profile_id} events={len(camp.events)} "
              f"epoch={camp.epoch:08x} cfg={camp.config_crc:08x} "
              f"dropped={camp.status.trace_dropped} aborted={camp.status.trace_aborted}")
        if args.out:
            events = [{"type": "trace", "epoch": e.epoch, "seq": e.seq,
                       "op": e.op, "ptr": e.ptr, "len": e.length,
                       "data": e.data.hex(), "trunc": 1 if e.truncated else 0}
                      for e in camp.events]
            meta = {
                "profile_id": camp.profile_id,
                "profile_digest": camp.profile_digest,
                "config_crc": camp.config_crc,
                "epoch": camp.epoch,
                "boot_nonce": camp.status.boot_nonce,
                "emitted": len(camp.events),
                "dropped": camp.status.trace_dropped,
                "aborted": camp.status.trace_aborted,
                "drained": camp.status.trace_drained,
                "device": device_label,
            }
            info = ev.write_campaign(args.out, meta, events,
                                     source=ev.SOURCE_HARDWARE, device=device_label,
                                     complete=True)
            print(f"raw campaign evidence: {info['path']} events={info['events']} "
                  f"digest={info['digest'][:16]}")
        for event in camp.events:
            print(json.dumps({"epoch": event.epoch, "seq": event.seq,
                              "op": event.op, "ptr": event.ptr,
                              "len": event.length, "data": event.data.hex(),
                              "truncated": event.truncated}, sort_keys=True))
    else:  # pragma: no cover - argparse enforces choices
        _err(f"unknown command {args.command!r}")
        return EXIT_ERROR
    return EXIT_OK


def _crc(p) -> int:
    import zlib
    return zlib.crc32(p.image) & 0xFFFFFFFF


def _print_status(st) -> None:
    print(f"armed={int(st.armed)} sealed={int(st.sealed)} loaded={int(st.loaded)} "
          f"expired={int(st.expired)} addr=0x{st.address:02x} "
          f"profile={st.profile_tag} crc=0x{st.image_crc:08x} "
          f"cfg=0x{st.config_crc:08x} epoch={st.epoch:08x} "
          f"lease_left_ms={st.lease_left_ms} emitted={st.trace_emitted} "
          f"dropped={st.trace_dropped} aborted={st.trace_aborted} "
          f"drained={st.trace_drained}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coralbench",
        description=f"Coralboard peripheral-impersonation bench ({MILESTONE}). "
                    "Simulation is the default; no hardware is touched unless "
                    "'serial' (explicit path) or 'usb' (exact USB serial) is used.")
    parser.add_argument("--version", action="version", version=f"coralbench {__version__} ({MILESTONE})")
    sub = parser.add_subparsers(dest="command", required=True)

    p_val = sub.add_parser("validate", help="validate a bounded register-map profile")
    p_val.add_argument("profile")
    p_val.set_defaults(func=cmd_validate)

    p_sim = sub.add_parser("simulate", help="record a deterministic simulation to JSONL")
    p_sim.add_argument("--profile", required=True)
    p_sim.add_argument("--ops", required=True, help="JSON file with an 'ops' list")
    p_sim.add_argument("--out", required=True, help="JSONL evidence path to write")
    p_sim.add_argument("--label", default=None)
    p_sim.add_argument("--queue-limit", type=int, default=256)
    p_sim.set_defaults(func=cmd_simulate)

    p_rep = sub.add_parser("replay", help="replay JSONL evidence against a profile")
    p_rep.add_argument("--profile", required=True)
    p_rep.add_argument("--evidence", required=True)
    p_rep.set_defaults(func=cmd_replay)

    p_ser = sub.add_parser("serial", help="talk to the Pico over an explicit device path")
    p_ser.add_argument("--device", required=True,
                       help="exact character device, e.g. /dev/ttyACM0 (never a glob)")
    p_ser.add_argument("--baud", type=int, default=115200)
    p_ser.add_argument("--timeout", type=float, default=2.0)
    ser_sub = p_ser.add_subparsers(dest="command", required=True)
    for name in ("status", "disarm", "drain"):
        ser_sub.add_parser(name)
    p_load = ser_sub.add_parser("load")
    p_load.add_argument("--profile", required=True)
    p_arm = ser_sub.add_parser("arm")
    p_arm.add_argument("--lease-ms", type=int, required=True)
    p_col = ser_sub.add_parser("collect")
    p_col.add_argument("--profile", required=True)
    p_col.add_argument("--out", default=None,
                       help="write raw structured campaign evidence (JSONL)")
    p_col.add_argument("--collect-timeout", type=float, default=30.0,
                       help="seconds to wait for the lease to seal while streaming")
    p_col.add_argument("--max-events", type=int, default=4096,
                       help="hard event budget; exceeding it fails closed")
    p_col.add_argument("--poll-ms", type=int, default=20,
                       help="streaming poll interval in milliseconds")
    p_ser.set_defaults(func=cmd_serial)

    p_usb = sub.add_parser(
        "usb", help="talk to the Pico over libusb CDC, selected by exact USB serial")
    p_usb.add_argument("--serial", required=True,
                       help="exact USB iSerialNumber, e.g. E660000000000000")
    p_usb.add_argument("--timeout", type=float, default=2.0,
                       help="per-command timeout in seconds")
    usb_sub = p_usb.add_subparsers(dest="command", required=True)
    for name in ("status", "disarm", "drain"):
        usb_sub.add_parser(name)
    p_uload = usb_sub.add_parser("load")
    p_uload.add_argument("--profile", required=True)
    p_uarm = usb_sub.add_parser("arm")
    p_uarm.add_argument("--lease-ms", type=int, required=True)
    p_ucol = usb_sub.add_parser("collect")
    p_ucol.add_argument("--profile", required=True)
    p_ucol.add_argument("--out", default=None,
                        help="write raw structured campaign evidence (JSONL)")
    p_ucol.add_argument("--collect-timeout", type=float, default=30.0,
                        help="seconds to wait for the lease to seal while streaming")
    p_ucol.add_argument("--max-events", type=int, default=4096,
                        help="hard event budget; exceeding it fails closed")
    p_ucol.add_argument("--poll-ms", type=int, default=20,
                        help="streaming poll interval in milliseconds")
    p_usb.set_defaults(func=cmd_usb)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except prof.ProfileError as exc:
        _err("profile: " + "; ".join(f"{i.code}: {i.message}" for i in exc.issues))
        return EXIT_ERROR
    except ev.EvidenceError as exc:
        _err(f"evidence: {exc}")
        return EXIT_ERROR
    except (ControllerError, ProtocolError, TraceIncomplete) as exc:
        _err(f"device: {exc}")
        return EXIT_ERROR
    except USBLinkError as exc:
        _err(f"usb: {exc}")
        return EXIT_ERROR
    except SerialLinkError as exc:
        _err(f"serial: {exc}")
        return EXIT_ERROR
    except ValueError as exc:
        _err(str(exc))
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
