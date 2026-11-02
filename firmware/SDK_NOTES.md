# Firmware build status and SDK assumptions

## Status: built, flashed and bench-validated on RP2040

`firmware/` (all four C sources plus the SDK and vmilea library) builds with
**arm-none-eabi-gcc 13.2.1**, Pico SDK **2.1.1**
(`bddd20f928ce76142793bef434d4f75f4af6e433`) and `pico_i2c_slave`
(`a25d1040950cdb4d37b50e05886c6195307f245c`), with no warnings in the
coralboard sources. Artifact: `coralboard_bench.uf2`, 87,040 bytes, SHA-256
`1b770d5b0680704abb93e6c24e3439bfd182f0fbd3c03891b82dbb41fb53dd3d`.

That exact image was flashed to a Pico and exercised against the TESTER
fixture. Rebuilding this source with the same inputs reproduces the hash
exactly. See `docs/hardware-validation.md` for the bench scope; the non-claims
there still apply.

| What | Verified? |
| --- | --- |
| `src/cmd.c` command parser | **Yes** — host compiler, 51 cases |
| `src/core.c` epochs, trace ring, drain cursor, lease seal/abort, pointer/wrap | **Yes** — host compiler, 84 cases |
| C core vs Python simulator parity (incl. cross-wrap reads) | **Yes** — parity test |
| CRC-32 matches `zlib.crc32` | **Yes** — standard check value asserted |
| `src/main.c`, `src/i2c_target.c` against the pinned SDK | **Yes** — ARM build, `-Wall -Wextra -Werror` |
| Alarm IRQ/priority, init order, stack frames | **Yes** — compiled ELF and `.su` output |
| I2C reads/writes, write mask, pointer wrap, re-arm, expiry NAK | **Yes** — bench-validated, exact image above |
| Electrical timing, clock stretching, wire-level ACK/NAK, sustained load | **No** |
| Compatibility with a real target device | **No** |
| Camera / NPU / vision | **No** |

## Alarm, IRQ and lease path

`add_alarm_in_ms()` uses the SDK **default alarm pool**. That pool claims
hardware alarm `PICO_TIME_DEFAULT_ALARM_POOL_HARDWARE_ALARM_NUM` (3 on RP2040),
so its IRQ is `hardware_alarm_get_irq_num(3)` = `TIMER_IRQ_3`, **not**
`TIMER_IRQ_0`. The firmware resolves the real IRQ with that API and sets:

```
irq_set_priority(CB_LEASE_ALARM_IRQ, 0x00);  /* IRQ 3 */
irq_set_priority(I2C0_IRQ,           0x40);  /* IRQ 23 */
```

Lower numbers are higher priority. The linked ELF disassembly shows
`irq_set_priority(3, 0)` followed by `irq_set_priority(23, 0x40)`.

The same path also:

1. treats `add_alarm_in_ms(...) <= 0` as failure (the pinned SDK returns 0 when
   the target time has passed or the pool is full) and seals/detaches with
   `ERR TIMER`;
2. schedules the alarm **before** `cb_target_attach()`, and runs the attach
   with interrupts masked only if the lease is still live; if expiry won, the
   target is sealed/detached instead of enabled;
3. cancels and clears the alarm in `DISARM` and in the main-loop backstop
   before any new lease;
4. guards the callback with `cb_core_expire_epoch(core, lease_epoch)`: a late
   callback from an older lease is ignored instead of sealing the current one
   (host-tested);
5. reads/snapshots core state under `save_and_disable_interrupts()`, and all
   core mutators take the same lock, so the I2C ISR and main loop cannot tear a
   transfer or a trace push.

The bench observed the intended external effect: after a 2000 ms lease, with no
SENSOR command during the wait, the next master request was NAKed and status
reported `armed=0 expired=1`. Electrical release latency was not measured.

## I2C init order

`cb_target_attach()` follows the pinned library's documented order:

```
gpio function/pulls
i2c_init(CB_I2C_INST, 100000)    /* reset, IC_CON, FIFOs, DMA, SDA hold, spike filter */
irq_set_priority(I2C0_IRQ, 0x40) /* re-assert after the block is reprogrammed */
i2c_slave_init(CB_I2C_INST, 0x48, bus_handler) /* slave mode + IRQ unmask */
```

`i2c_slave_init()` alone only calls `i2c_set_slave_mode()` and unmasks the IRQ;
it does not apply the SDK's fast-mode SDA-hold/spike-filter configuration.
Clock-stretch and ACK timing remain non-claims.

## Stack budget

`char line[1024]` and `cb_cmd` (~340 B) live in `.bss`, and
`PICO_STACK_SIZE=0x2000` is set explicitly in `CMakeLists.txt` (confirmed by
`StackSize = 0x2000` and `__StackTop - __StackBottom = 0x2000` in the linked
ELF/map).

Measured with the production `-fstack-usage` build (not an estimate):

```
main                       72 bytes
handle_command.constprop  224 bytes
cb_parse_line             240 bytes
split_kv.constprop         40 bytes
cb_core_bus_read           24 bytes
bus_handler                16 bytes
```

Worst synchronous chain `main -> handle_command -> cb_parse_line -> split_kv`
is 576 B; the I2C ISR chain adds roughly 100 B including the hardware frame.
Against 8,192 B this is a large margin.

## FINISH semantics — checked, not assumed

The pinned `i2c_slave.c` raises `I2C_SLAVE_FINISH` when any of `TX_ABRT`,
`START_DET` or `STOP_DET` is set and a transfer is in progress. So FINISH
closes a **segment**, not necessarily a whole START..STOP transaction:

- `write(pointer) Sr read(n)` produces two FINISH callbacks (pointer segment at
  the repeated START, read segment at STOP), hence two trace events.
- The library does not report which terminator fired. A repeated-START **write**
  continuation cannot be distinguished from a new pointer write, so it is
  declared unsupported; masters use STOP before data writes.
- TX_ABRT also arrives as FINISH. The core treats it as the end of a segment;
  the adapter cannot distinguish it from STOP, so bus-level aborts are not
  counted as `aborted`. `aborted` counts transfers cut by DISARM or lease
  expiry (the cases this firmware controls).

## Behaviour the source guarantees

1. **Boot unarmed**: `cb_target_pins_high_z()` runs before `stdio_init_all()`;
   GP4/GP5 are function-NULL inputs with pulls disabled.
2. **Pins only alive while armed**: `cb_target_attach()` runs only in the ARM
   handler and only after a successful alarm schedule and a live-lease check.
3. **Lease expiry independent of USB**: the alarm IRQ is above I2C; its callback
   only parks GP4/GP5, disables the bus IRQ and seals the epoch, then defers
   `i2c_slave_deinit()` to main context. A blocked USB `printf` cannot keep the
   target attached.
4. **Bounded lease**: parser and core reject `0` and `> 10000`; failure to
   schedule fails closed; the main loop keeps a cancel-and-seal backstop.
5. **Interrupt path never blocks**: the I2C handler only touches the image and
   a bounded ring. No `printf`, allocation, stdio or sleep.
6. **Epoch-bound bounded trace**: 256-segment ring; overflow increments a
   saturating `dropped`; disarm/expiry mid-segment increments `aborted`.
7. **Streaming drains**: `TRACE DRAIN` is allowed while armed and reports
   `sealed=0`; `TRACEEND complete=1` requires sealed, unarmed, no drops/aborts,
   empty ring, at least one event and `drained==emitted`.
8. **Fresh ARM resets state**: template image, pointer = `init`, counters and
   transfer state cleared; new epoch; a stale alarm cannot seal it.
9. **Load only while disarmed and with no undrained trace**: `ERR ARM_ACTIVE`
   or `ERR TRACE_PENDING`; it never silently discards events.

## Known gaps

- No electrical/timing observation: ACK/NAK at the wire, clock stretching, pin
  high-Z voltage, USB CDC jitter and expiry during an in-progress transfer are
  unmeasured.
- Multi-master arbitration and bus errors are not modelled. TX_ABRT is not
  distinguishable through the library's FINISH event.
- Repeated-START write continuation is unsupported (see above).
- The internal pull-ups (~50 kΩ) are a backstop only; the fixture must fit
  external pull-ups to 3.3 V. The verified bench used two 5.1 kΩ resistors.
- `boot_nonce` is derived from `time_us_32()` at boot, not a random/unique ID;
  cross-boot epoch collision is possible and is not claimed to be impossible.
- No real target device has been connected; no device fidelity is claimed.
