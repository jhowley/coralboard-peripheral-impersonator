# Scope — milestone 1 delivered vs deferred

This is the honest boundary of what exists today. Anything below the line has
**no implementation**, not a stub with a hopeful comment.

## Delivered and verified

| Capability | Where | Verified how |
| --- | --- | --- |
| Bounded register-map profiles, interchangeable, fidelity-claiming rejected | `src/coralbench/profile.py`, `profiles/*.json` | offline unit tests |
| Deterministic I2C target simulator (pointer, write window, wrap, trace ring) | `src/coralbench/simulator.py` | offline unit tests |
| Byte-level JSONL evidence with digest + tamper detection | `src/coralbench/evidence.py` | offline unit tests |
| Deterministic replay / mismatch reporting | `src/coralbench/replay.py` | offline unit tests |
| Explicit-path serial transport (termios/select, no discovery, no flush-on-open) | `src/coralbench/serial_link.py` | unit tests on a real pty |
| Userspace libusb CDC transport; exact-serial selection, descriptor verification, no detach/reset | `src/coralbench/usb_link.py` | fake-backend tests **and one live two-board bench session** |
| Host controller: lease bound, load-while-armed refusal, drain/completeness gate | `src/coralbench/controller.py` | labelled fake + live bench |
| CLI: validate / simulate / replay / serial / usb (status, load, arm, disarm, drain, collect) | `src/coralbench/cli.py` | subprocess/pty tests, fake backend, live `usb` session |
| Offline runner that denies sockets before imports | `tests/offline_runner.py` | unit test of the audit hook |
| Firmware command parser, host-compiled | `firmware/src/cmd.c` | 51 host cases |
| Firmware state/trace/lease core, host-compiled | `firmware/src/core.c` | 84 host cases + C/Python parity |
| Firmware SDK glue (alarm expiry, stdio loop, I2C adapter), high-Z release | `firmware/src/main.c`, `i2c_target.c` | built, flashed and exercised at the bench |
| TESTER master firmware: bounded `PING`/`STATUS`/`W`/`R`/`WR`, 90 ms budget, bus-clear | `tester/src/` | 100 host checks, flashed and exercised at the bench |
| TESTER client: identity gate, one explicit command, libusb fallback | `tester/client/tester_client.py` | 6 offline checks, live bench session |

See [`hardware-validation.md`](hardware-validation.md) for the exact bench
results and their limits.

## Deferred (out of milestone 1)

- **Real target-device tests.** No real sensor or peripheral was on the bus.
  Nothing is claimed about any device's behaviour, and the profiles are not
  device models.
- **Electrical characterisation.** No scope/logic-analyser timing, ACK/NAK wire
  capture, clock-stretch, arbitration or stuck-bus measurements.
- **SPI impersonation.** No profile schema, no firmware, no host path.
- **I3C.** Not started; requires target-role controller support this board does
  not have.
- **Camera and NPU integration.** No image capture, vision pipeline or
  acceleration path. These peripherals exist on the Coralboard but are not
  used by this project.
- **Power and reset control.** Deliberately absent: no relay, rail switching,
  reset line or soft-reset command.
- **Radio work.** Absent. The ESP32-C5 wireless endpoint is a roadmap proposal,
  not implemented code.
- **Raw hardware segments → replayable transaction session.** `collect --out`
  writes a raw campaign with epoch, config and counts. Converting those
  segments into the transaction-session schema used by `replay` is a separate,
  unimplemented step.
- **Tigard companion controller / debug / voltage adapter.** Roadmap only; see
  [`roadmap.md`](roadmap.md). It is **not** a drop-in sensor replacement.

## Deliberate non-claims

1. These profiles do not model a TMP102 or any other part. A profile is a
   bounded byte image plus a policy; validation rejects `fidelity` values other
   than `synthetic-bounded` and any non-null `claimed_device`.
2. A passing `replay` proves the recorded bytes are consistent with the profile
   — not that a device behaved correctly.
3. The bench proved address-level behaviour and deterministic bytes on one
   wiring/build. It did not prove timing margins or compatibility with a real
   target.
4. Repeated-START reads are modelled as two segments; repeated-START write
   continuation is unsupported because the target library does not report STOP
   versus repeated START.
5. The trace records bytes prepared by the target, not ACK/NAK wire events.
