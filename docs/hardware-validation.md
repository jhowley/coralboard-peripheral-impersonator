# Hardware validation — historical bench record

**Status: historical snapshot of one bench session on 2026-11-02.** It is not
reproduced by CI and none of the raw logs, serial numbers, host addresses or
machine paths from that session are published. This page records what was
observed, on which build, and what remains unproven.

## Builds under test

| Role | Firmware | Size | SHA-256 of UF2 |
| --- | --- | --- | --- |
| SENSOR / impersonator | `coralboard_bench` | 87,040 B | `1b770d5b0680704abb93e6c24e3439bfd182f0fbd3c03891b82dbb41fb53dd3d` |
| TESTER / master | `coralboard_tester` | 86,528 B | `2915b3349f6aebe41887dabfed5d9b37089ed39c84a07d7b7491e4053f2e1f4c` |

Both were built with Pico SDK 2.1.1, `pico_i2c_slave`
`a25d1040950cdb4d37b50e05886c6195307f245c` and arm-none-eabi-gcc 13.2.1.
Both UF2s were flashed to original Raspberry Pi Pico boards. Rebuilding the
source in this repository with the same inputs reproduces both hashes exactly.

Wiring was the one documented in [`wiring.md`](wiring.md): SDA on GP4
(physical pin 6), SCL on GP5 (pin 7), common GND on pin 8, two 5.1 kΩ pull-ups
to the TESTER's 3V3(OUT) pin 36, separate USB power for each board, no 5 V and
no joined 3V3 rails. The owner checked continuity and measured approximately
3.28 V on the supply and both idle lines before the first test.

## First wired test (historical)

- Loaded profile `bench-temp-raw-v1`; reported `cfg=0x934c509b`,
  image CRC `0x9f9dc8ab`.
- Armed the SENSOR for 5000 ms and issued one `WR 00 4` from the TESTER.
- Read back `19604b50`, the first four profile bytes.
- The SENSOR reported `armed=0 sealed=1 emitted=2 dropped=0 aborted=0` and a
  two-segment trace: `seq0` pointer write (`len=0`), `seq1` read of four bytes.
- The TESTER reported `ops=1 ok=1 err=0 timeouts=0 last=ok`.

## Full wired validation (historical)

Twenty scripted checks passed across 17 bus operations in this run:
**16 successful operations and one expected NAK after the lease expired**, with
no unexpected errors. Cumulative TESTER counters at the end were `ops=18
ok=17 err=1 timeouts=0 last=ok`, where the extra operation is the earlier
first wired test.

Observed results:

- Five independent arm → pointer-write → four-byte-read → disarm cycles all
  returned `19604b50`; each produced matching two-segment traces.
- Writable-register patterns `000000`, `ffffff` and `aa55a5` (written from
  register 1) read back as `19000000`, `19ffffff` and `19aa55a5`.
- A write to read-only register 0 left the first byte `19` unchanged.
- A four-byte read from pointer 3 returned `5019604b`, i.e. the bounded
  four-byte wrap (`50` then `19 60 4b`), not an out-of-range read.
- Re-arming restored the profile template.
- Expiry test: armed for 2000 ms, one read succeeded, then the harness slept
  2.3 s without sending any SENSOR command. The next TESTER request returned
  `ERR NAK phase=w at=0` **before** SENSOR status was queried; the SENSOR then
  reported `armed=0 expired=1`. A fresh arm and read succeeded afterwards.

Trace accounting: 28 segments parsed, none truncated; intermediate status
checks reported `dropped=0 aborted=0`; the final SENSOR state was `armed=0
sealed=1 dropped=0 aborted=0 drained=emitted=2`. Both boards were left powered
and idle, SENSOR disarmed. No firmware was changed during the run.

The session used the userspace libusb CDC transport on both boards (the host
had no `cdc_acm` device node), so this also exercised the live `usb` transport
path and the TESTER client's `--bench-src` libusb fallback.

## What this proves

- The flashed SENSOR firmware answers I2C reads at `0x48` with the configured
  bounded bytes, applies the declared write window, wraps reads at the
  declared modulus, restores the template on re-arm, and stops acknowledging
  its address after the lease expires.
- The TESTER firmware performs the documented write, read and
  write-with-repeated-START-read transactions at 100 kHz and reports actual
  byte counts.
- The host tooling can load a profile, arm, collect and drain traces over the
  live USB transport, and can reproduce the deterministic profile bytes.
- The two builds above are exactly reproducible from this source tree with the
  pinned SDK inputs.

## What this does **not** prove

- No real target device was on the bus. No vendor device was impersonated and
  no claim is made that these bytes satisfy any real sensor.
- No oscilloscope or logic-analyser timing was captured. ACK/NAK counting is
  the firmware's completed-byte accounting, not a wire capture.
- Expiry was observed as address NAK after the lease; the exact electrical
  release latency and expiry during an in-progress transfer were not measured.
- Sustained load, stuck-bus recovery under a hostile target, power-loss
  behaviour, multi-master arbitration and hot-plug were not tested.
- The camera and NPU on the Coralboard were not involved. No vision or
  acceleration path was exercised.
- No Tigard, ESP32-C5 or other companion hardware was tested; those are
  roadmap proposals only (see [`roadmap.md`](roadmap.md)).
