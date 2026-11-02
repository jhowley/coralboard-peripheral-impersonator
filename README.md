# Coralboard peripheral impersonator

A programmable stand-in for sensors, built for repeatable hardware testing.
The Coralboard SL coordinates experiments over USB; a Raspberry Pi Pico
(RP2040) supplies controlled I2C register values, and a second Pico verifies
them. The host tools record hardware traces and can replay simulated sessions.
Hardware-trace conversion into replayable sessions is not implemented yet.

One Pico runs the impersonator firmware and answers at I2C address `0x48` only
while a time-bounded lease is armed. A second Pico runs a minimal **TESTER**
master that issues one explicit bus operation per command. A stdlib-only
Python host loads a profile, arms the target, collects a trace and proves
whether the session is byte-complete.

**Status: experimental public prototype.** It has been built, flashed and
exercised on a real two-Pico bench. It is **not** a validated emulator of any
commercial sensor, and it does not claim TMP102 or any other device fidelity.

## What is actually verified

| Area | State |
| --- | --- |
| Python profile, simulator, evidence, replay, controller, serial/USB transports | **verified offline** — 189 tests, network denied |
| Sensor firmware parser, state/trace core, pointer/wrap semantics | **verified on host** — 51 parser cases, 84 core cases, C/Python parity |
| Sensor firmware on RP2040 | **built, flashed, bench-tested** — exact UF2 hash reproduced from this tree |
| TESTER firmware on RP2040 | **built, flashed, bench-tested** — 100 host checks, exact UF2 hash reproduced |
| Live USB CDC transport (libusb path) | **verified in one bench session** on both boards |
| Real IoT controller in place of the TESTER | **not tested** — no real device was on the bus |
| Electrical timing, clock stretching, ACK/NAK wire capture, sustained load | **not tested** |
| Camera / NPU / vision | **not integrated** |
| Tigard, ESP32-C5 companions | **roadmap only** — no code |

Bench summary: 17 bus operations, 16 successful and one expected NAK after the
lease expired; five arm/read/disarm cycles matched the profile; writes obeyed
the declared window; reads wrapped at the declared modulus; re-arm restored the
template. Full record and limits:
[`docs/hardware-validation.md`](docs/hardware-validation.md).

Verified builds (UF2 SHA-256):

```
coralboard_bench    87,040 B  1b770d5b0680704abb93e6c24e3439bfd182f0fbd3c03891b82dbb41fb53dd3d
coralboard_tester   86,528 B  2915b3349f6aebe41887dabfed5d9b37089ed39c84a07d7b7491e4053f2e1f4c
```

Rebuilding this source with the pinned inputs below reproduces both hashes
exactly.

## How it works

- **Profiles** declare a 256-byte image, an address, a writable mask, a pointer
  wrap modulus, an auto-increment policy and a lease ceiling. Validation
  accepts only `fidelity: "synthetic-bounded"` and `claimed_device: null`.
- **ARM** restores the image from the loaded template, starts a new epoch and
  enables the I2C target for at most 10,000 ms. `DISARM`, lease expiry or boot
  leaves GP4/GP5 high-impedance with pulls disabled.
- **Trace** records one event per bus segment in a bounded ring with epoch and
  sequence numbers, plus saturating `dropped`/`aborted` counters. A campaign is
  complete only when one epoch has contiguous sequence numbers, no drops, no
  aborts, no truncated events and all events were drained by that collect.
- **Replay** re-runs the recorded transactions against the same profile and
  reports per-byte mismatches. It proves consistency with the profile, not
  device correctness.

Deliberate limits: repeated-START reads are modelled as two segments;
repeated-START write continuation is unsupported because the target library
does not distinguish STOP from repeated START. The trace records bytes the
target prepared, not wire-level ACK/NAK events.

## Repository layout

```
src/coralbench/       host library and CLI (stdlib only)
profiles/             two interchangeable bounded register maps
firmware/             impersonator firmware, SDK glue and host tests
tests/                offline unittest suite + labelled fakes
scripts/              portable build and test entry points
docs/                 wiring, protocol, operator guide, scope, validation, roadmap
tester/               second-Pico TESTER firmware, client and host tests
```

## Quick start — no hardware

Requires Linux, Python 3.10+, make and a C compiler. No SDK, no network, no board.
Clone this repository and run these commands from its root directory.

```sh
make test
```

This runs the Python suite under a network-denying audit hook, the host-compiled
firmware parser/core tests, the TESTER host tests and the TESTER client tests.

Simulate and replay a session:

```sh
PYTHONPATH=src python3 -m coralbench simulate \
    --profile profiles/bench-temp-raw-v1.json \
    --ops tests/fixtures/ops-smoke.json \
    --out /tmp/rehearsal.jsonl

PYTHONPATH=src python3 -m coralbench replay \
    --profile profiles/bench-temp-raw-v1.json \
    --evidence /tmp/rehearsal.jsonl
```

Simulated output is labelled `SIMULATED` in both stdout and the evidence
header, so it cannot pass as hardware data.

## Wiring

Three wires and exactly two resistors:

| Signal | TESTER | SENSOR |
| --- | --- | --- |
| SDA | GP4, physical pin 6 | GP4, physical pin 6 |
| SCL | GP5, physical pin 7 | GP5, physical pin 7 |
| GND | GND, physical pin 8 | GND, physical pin 8 |
| SDA pull-up | 5.1 kΩ to 3V3(OUT) pin 36 | — |
| SCL pull-up | 5.1 kΩ to 3V3(OUT) pin 36 | — |

Each board is powered from its **own** USB cable. Do not join the two 3V3
rails, and do not connect VBUS or VSYS between boards. **3.3 V logic only; the
Pico is not 5 V tolerant.** Full schematic, forbidden connections and
pre-flight checklist: [`docs/wiring.md`](docs/wiring.md).

## Building the firmware

The SDK and I2C target library are build inputs, not vendored. Verified
versions:

- Pico SDK **2.1.1**, commit `bddd20f928ce76142793bef434d4f75f4af6e433`
- `vmilea/pico_i2c_slave`, commit `a25d1040950cdb4d37b50e05886c6195307f245c`
- arm-none-eabi-gcc **13.2.1**

```sh
export PICO_SDK_PATH=/path/to/pico-sdk
export PICO_I2C_SLAVE_PATH=/path/to/pico_i2c_slave
export PICO_TOOLCHAIN_PATH=/path/to/arm-none-eabi   # optional if on PATH

make build-sensor    # build/firmware-sensor/coralboard_bench.uf2
make build-tester    # build/firmware-tester/coralboard_tester.uf2
```

Outputs are ignored by git. Flashing is a separate, deliberate step; the
tooling has no flash, power or reset control.

## Talking to the boards

Select each board explicitly; USB enumeration is used to match its serial,
not to probe the I2C bus. Set `SENSOR_SERIAL` and `TESTER_SERIAL` to the actual
USB serials of your boards (for example, inspect `lsusb -v` on Linux).
Use exactly one transport:

```sh
# Kernel CDC ACM exists
PYTHONPATH=src python3 -m coralbench serial --device /dev/ttyACM0 status

# Userspace libusb, selected by the exact USB serial reported by device enumeration
PYTHONPATH=src python3 -m coralbench usb --serial "$SENSOR_SERIAL" status
```

The TESTER client defaults to `PING` + `STATUS` only — no bus transaction.
Add at most one explicit command:

```sh
python3 tester/client/tester_client.py --serial "$TESTER_SERIAL" \
    --bench-src "$PWD/src" --write-read 00 4
```

`--bench-src` points at **this repository's** `src/` directory and is used only
for the read-only libusb fallback when no CDC ACM device node exists. The
client refuses to send a bus command unless the first reply is exactly
`OK PING TESTER fw=1.0.0`.

Protocols: sensor [`docs/protocol.md`](docs/protocol.md), TESTER
[`tester/docs/protocol.md`](tester/docs/protocol.md). Operating procedure:
[`docs/operator-guide.md`](docs/operator-guide.md).

## Roadmap and safety

See [`docs/roadmap.md`](docs/roadmap.md). Headline proposals:

- **Tigard companion** for control, debug and voltage adaptation. It is **not**
  a drop-in sensor replacement; its own voltage settings need a power audit.
  It is not a general-purpose inline level shifter for the Pico.
- **ESP32-C5 wireless endpoint** for untethered control. The C5 is **not 5 V
  safe** at its GPIO. A development board may accept USB power through its
  onboard regulator; signal levels still need checking.
- Future work: level shifting, camera/vision observation, SPI/I3C exploration.

This is a research bench, not a production test system. Do not connect it to
equipment you cannot afford to damage, and read the wiring rules before
powering anything.

## Contributing, security, licence

- [`CONTRIBUTING.md`](CONTRIBUTING.md)
- [`SECURITY.md`](SECURITY.md)
- MIT licence, Copyright (c) 2026 Josh Howley — [`LICENSE`](LICENSE)
- Third-party build inputs and their licences:
  [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)
