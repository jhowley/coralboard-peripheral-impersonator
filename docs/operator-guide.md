# Operator guide — milestone 1 bench session

Audience: whoever is at the bench with two Picos, the documented 3.3 V wiring
and a meter. Read `docs/wiring.md` first; nothing here protects against 5 V.

## 0. Roles

- **Host** — this repository, `python3 -m coralbench`. Talks to the Pico over
  USB CDC, records evidence, decides whether a campaign is usable.
- **Pico A / SENSOR** — runs the impersonator firmware, impersonates `0x48`
  only while a lease is armed.
- **Pico B / TESTER** — runs the included `tester/` firmware and generates one
  explicit I2C transaction per command. Other compatible 3.3 V controllers may work, but
  only this TESTER has been qualified.
- **Simulator** — the default path, no hardware. Everything below that touches
  `/dev/ttyACM*` is optional and explicit.

## 1. Offline rehearsal (do this first, every time)

```sh
sh scripts/run_offline_tests.sh

PYTHONPATH=src python3 -m coralbench simulate \
  --profile profiles/bench-temp-raw-v1.json \
  --ops tests/fixtures/ops-smoke.json \
  --out /tmp/rehearsal.jsonl
PYTHONPATH=src python3 -m coralbench replay \
  --profile profiles/bench-temp-raw-v1.json --evidence /tmp/rehearsal.jsonl
```

Expected: `offline run: tests=189 failures=0 errors=0`, `51 cases, 0 failure(s)`
for the parser, `84 cases, 0 failure(s)` for the core, plus the TESTER host and
client suites, and `replay PASS`. If any of that fails, stop — the bench
tooling is the problem, not the board.

## 2. Profile choice

Two interchangeable bounded profiles ship here:

| Profile | Defaults | Writable | Auto-increment | Lease cap |
| --- | --- | --- | --- | --- |
| `bench-temp-raw-v1` | `19 60 4b 50` | regs 1–3 | yes, wrap 4 | 10 000 ms |
| `bench-temp-alt-v1` | `15 00 40 45` | regs 2–3 | no | 5 000 ms |

Swapping profiles must change what a master reads; that is the point of the
`--profile` argument on both `simulate` and `replay`. A recorded session only
replays against the profile whose digest is in its header (cross-profile replay
fails loudly, with per-byte mismatches).

## 3. Attach to the board (explicit selection only)

The CLI never scans for or guesses a device. Choose exactly one transport.

**A. Kernel CDC ACM path** — only if the kernel bound a driver and
`/dev/ttyACM*` exists. Find the path yourself (`ls /dev/ttyACM*` before and
after plugging in) and pass exactly one:

```sh
PYTHONPATH=src python3 -m coralbench serial --device /dev/ttyACM0 status
```

**B. Userspace libusb path** — required on hosts that expose
`libusb-1.0.so.0` but do not bind `cdc_acm`, so no `/dev/ttyACM*` appears.
Select the device by its exact USB serial number (read the serial from USB enumeration on
your host); there is no bus/address or glob option:

```sh
PYTHONPATH=src python3 -m coralbench usb --serial "$SENSOR_SERIAL" status
```

`usb` opens the device read/write only after verifying the observed
descriptors (`2e8a:000a`; interface 0 CDC-ACM `02/02/00` with interrupt IN
`0x81` and an 8-byte packet size; interface 1 CDC-Data `0a/00/00` with bulk
OUT `0x02` and IN `0x82`, 64-byte packets). It
claims interfaces 0 and 1 only, never interface 2, never detaches an active
kernel driver, never resets the device and never enters the bootloader. A
different descriptor layout, a missing serial, or more than one device with
that serial is an error, not a fallback. CDC line coding is fixed at
115200 8N1 with DTR/RTS asserted; the line protocol above the transport is
unchanged.

`status` writes one command and reads the reply; it does not arm anything, and
opening the port flushes nothing, so attaching mid-lease cannot disturb a
running session.

First real bring-up, short leash (use `usb --serial <your-serial>` in
place of `serial --device /dev/ttyACM0` when using transport B):

```sh
PYTHONPATH=src python3 -m coralbench serial --device /dev/ttyACM0 \
  load --profile profiles/bench-temp-raw-v1.json
PYTHONPATH=src python3 -m coralbench serial --device /dev/ttyACM0 \
  arm --lease-ms 2000
# ... master traffic happens here ...
PYTHONPATH=src python3 -m coralbench serial --device /dev/ttyACM0 disarm
PYTHONPATH=src python3 -m coralbench serial --device /dev/ttyACM0 drain
```

`load` refuses while armed (`ERR ARM_ACTIVE`); `arm` refuses without a loaded
image (`ERR NOT_LOADED`); `arm` refuses leases outside 1..10 000 ms **before**
sending anything.

## 4. Harvesting a campaign (streaming while armed)

`collect` is the only verb that produces a verdict, and the primary workflow is
to issue it **while the lease is armed**. It drains the same epoch in bounded
rounds, keeps the contiguous stream, waits (with a finite timeout) for the
lease to seal, then asks `TRACE END` and validates before writing anything:

```sh
# arm first, then start the collect while the target is live
PYTHONPATH=src python3 -m coralbench serial --device /dev/ttyACM0 \
  arm --lease-ms 5000
PYTHONPATH=src python3 -m coralbench serial --device /dev/ttyACM0 \
  collect --profile profiles/bench-temp-raw-v1.json --out campaign.jsonl \
  --collect-timeout 30 --max-events 4096
```

`collect` accepts either state:

- **armed**: streams the current epoch until the lease seals (or the
  `--collect-timeout` deadline / `--max-events` budget is hit, in which case it
  fails closed and writes nothing),
- **already sealed and undrained**: collects the tail in one pass.

A successful collect requires loaded profile tag, image CRC and `cfg` matching
the profile file; one epoch throughout; sequence numbers exactly
`0..emitted-1`; no drops, aborts or truncated events; `drained == emitted`; and
`TRACE END` reporting `sealed=1 dropped=0 aborted=0 complete=1`. A standalone
`drain` before a collect consumes the cursor and disqualifies that epoch — use
`collect` itself while armed instead of draining first.

Any of those failing raises and prints why. A campaign that is "mostly there"
is not a campaign: it is a rerun.

With `--out`, a successful collect also writes **raw campaign evidence**: a
header with profile digest, `cfg`, epoch, boot nonce and validated counts, one
record per bus segment, and a footer with digests. This is not a replayable
transaction session; converting segments to replay transactions is a separate,
explicit step and is not claimed by this tool.

### Why a trace can be refused

| Symptom | Cause | Action |
| --- | --- | --- |
| `trace overflow: N dropped event(s)` | ring (256) filled because the host drained too late | shorten the lease, use streaming `collect`, rerun |
| `N aborted transfer(s)` | disarm/expiry cut a segment in progress | rerun with a clean stop |
| `trace already partially drained` | a standalone `drain`, or an earlier collect, consumed events | start a new lease and let `collect` stream it from seq 0 |
| `trace event was truncated` | a segment was longer than 8 bytes, so the byte trace is not exact | rerun with reads ≤ 8 bytes |
| `epoch changed mid-collect` | a reload/re-arm happened during harvest | start over |
| `streaming collect timed out` | the lease never sealed inside `--collect-timeout` | check the board clock/alarm; rerun with a fresh lease |
| `loaded profile 'x' is not the requested 'y'` | stale image on the board | `load` the intended profile |
| `image crc ... != profile crc ...` | board image and profile file disagree | reload; treat the session as void |

## 5. Reading the numbers

- `epoch` — `(boot nonce << 16) | counter`; changes on every `LOAD` and `ARM`.
- `cfg` — CRC over tag, image, wmask, flags, wrap and initial pointer.
- `emitted` / `dropped` / `aborted` / `drained` — counts for the **current
  epoch**. `drained` is the host-visible cursor; a qualifying collect ends with
  `drained == emitted`.
- `len` vs `data` in a `T` line: `len` is the full segment byte count (256 is
  legal); `data` is the first ≤ 8 bytes with `trunc=1` when longer. This is
  what the target prepared, not an ACK/NAK wire capture.

## 6. Stopping

- `DISARM` — target deinitialised, GP4/GP5 parked high-impedance.
- Do nothing and wait — the lease-expiry alarm runs on
  `hardware_alarm_get_irq_num(PICO_TIME_DEFAULT_ALARM_POOL_HARDWARE_ALARM_NUM)`
  (IRQ 3 on RP2040) at priority `0x00`, above I2C at `0x40`; it parks the pins
  even if USB output is blocked or the cable is pulled. Full deinit runs
  afterwards in the main loop. The alarm is scheduled before the bus is
  enabled, and a late callback for an older epoch is ignored.
- Unplug USB — the Pico reboots unarmed; pins are never driven at boot.

There is no power control and no reset control in this tooling; use the bench
supply and the documented power-off/BOOTSEL recovery procedure.

## 7. What to hand to the reviewer

- The console output from `sh scripts/run_offline_tests.sh`.
- The JSONL from `simulate`, plus its `replay PASS` line.
- For hardware work: the `serial ...` or `usb ...` status/collect transcript and the collected trace segments, with the device path and the profile digest named. Anything
  without a profile digest and a `dropped=0` verdict is a rehearsal, not
  evidence. Do not publish raw captures that contain board serials or host
  addresses.
