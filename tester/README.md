# TESTER — second-Pico I2C master fixture

Minimal RP2040 firmware that turns a Raspberry Pi Pico into an I2C **master**
for the impersonator bench. It answers bounded ASCII commands on the default
Pico USB CDC port and performs exactly one bus transaction per command at
address `0x48`, I2C0, GP4/GP5, 100 kHz.

The TESTER is deliberately not part of the sensor firmware and has no scan,
address parameter, background timer or boot-time transaction.

## Status

- Host behavioural tests: **100 checks, 0 failures**.
- Client offline tests: **6 checks, 0 failures**.
- Cross-build: clean with `-Wall -Wextra -Werror`; UF2 SHA-256
  `2915b3349f6aebe41887dabfed5d9b37089ed39c84a07d7b7491e4053f2e1f4c`
  (86,528 bytes), reproducible from this tree.
- **Flashed and exercised on a real two-Pico bench** — ordinary writes, reads
  and write-with-repeated-START-read transactions, including the expected NAK
  after the impersonator's lease expired. See
  [`../docs/hardware-validation.md`](../docs/hardware-validation.md).
- Abort timing, bus-clear against a genuinely stuck target and USB CDC latency
  are still unmeasured.

## Layout

```
CMakeLists.txt            SDK build; default CDC descriptor, no extra interfaces
build.sh                  portable helper: requires PICO_SDK_PATH
src/tester_core.h/.c      SDK-free parser, dispatch and reply formatting
src/i2c_master.h/.c       bounded I2C0 master with cleanup on every exit path
src/main.c                USB line loop only
client/tester_client.py   stdlib identity check / one explicit command
client/test_client_offline.py
tests/test_core.c         behavioural tests for the core
tests/run_tests.sh        host build + run
docs/protocol.md          exact commands, replies and limits
docs/wiring.md            pointer to the canonical wiring
docs/sdk-timeout-notes.md why the SDK timeout helpers are not used
```

## Build

Requires `PICO_SDK_PATH` (2.1.1 verified) and an `arm-none-eabi` toolchain,
either on `PATH` or at `PICO_TOOLCHAIN_PATH`.

```sh
export PICO_SDK_PATH=/path/to/pico-sdk
export PICO_TOOLCHAIN_PATH=/path/to/arm-none-eabi   # optional if on PATH
./build.sh
# -> build/coralboard_tester.uf2
```

From the repository root, `make build-tester` uses the shared portable script
and writes `build/firmware-tester/coralboard_tester.uf2`.

## Host tests

```sh
./tests/run_tests.sh                 # 100 checks, needs only a host C compiler
python3 client/test_client_offline.py  # 6 checks, no device access
```

## Client

`PING` + `STATUS` only is the default — no bus transaction:

```sh
python3 client/tester_client.py --serial <16-hex-serial>
```

One explicit bus command may be added:

```sh
python3 client/tester_client.py --serial <16-hex-serial> \
    --bench-src ../src --write-read 00 4
```

`--bench-src` points at the **repository's own** `src/` directory (for example
`--bench-src "$PWD/src"` when run from the repository root). It is used only
when no CDC ACM device node exists and the client falls back to the read-only
`coralbench.usb_link.USBLink` over libusb. The client refuses to send any bus
command unless the first reply is exactly `OK PING TESTER fw=1.0.0`.

The client selects a device only by exact USB serial; it does not glob, scan or
guess.

## Protocol

| Command | Transaction |
| --- | --- |
| `PING` | none; identity/version |
| `STATUS` | none; counters and fixed configuration |
| `W <hex>` | write 1..32 bytes |
| `R <n>` | read 1..32 bytes |
| `WR <hex> <n>` | write, repeated START, read |

Full grammar, reply fields and error codes: [`docs/protocol.md`](docs/protocol.md).

## Safety properties

- GP4/GP5 are SIO inputs with pulls disabled before `stdio_init_all()` and
  after every command.
- The I2C block is enabled only inside an explicit bus command.
- 90 ms transfer budget, bounded abort and bounded bus-clear; a failed command
  returns in under 100 ms in the intended design.
- Reply counts are the bytes actually moved; a short-count "success" becomes
  `ERR BUS`, never `OK`.
- Internal pull-ups are off; the fixture requires external 5.1 kΩ pull-ups to
  one 3.3 V source (TESTER `3V3(OUT)` pin 36).
- 3.3 V logic only. Do not connect a 5 V bus or join the two 3V3 rails.

## Non-claims

- No real target device has been used; this is a second-Pico fixture.
- No electrical timing, arbitration or stuck-bus measurement.
- A target that holds SCL low indefinitely cannot be forced to release by this
  firmware; recovery is bounded and reports `ERR TIMEOUT`, but the next command
  may still fail until the target lets go.
