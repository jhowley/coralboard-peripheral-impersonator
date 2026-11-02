# SDK 2.1.1 I2C master timeout behaviour and why this port does not use it

Checked against Pico SDK 2.1.1 (commit `bddd20f9`, "SDK 2.1.1 Release"),
file `src/rp2_common/hardware_i2c/i2c.c` in the SDK checkout supplied at build
time.

## What the SDK helpers do on failure

`i2c_write_timeout_us()` is `i2c_write_blocking_until()` with one absolute
deadline (`hardware/i2c.h:280-283`). On a per-byte wait it sets a local
`timeout`/`abort` flag and breaks out. Then:

- it does **not** set `IC_ENABLE.ABORT`, does **not** disable the block and
  does **not** call `i2c_deinit()`;
- it leaves `i2c->restart_on_next = nostop`, so the next call starts with a
  repeated START if the failed call had `nostop=true`;
- it returns `PICO_ERROR_TIMEOUT`; FIFO/abort state is whatever the hardware
  happened to leave.

`i2c_read_timeout_us()` (`i2c.c:250-345`) has an additional unbounded wait:

```c
while (!i2c_get_write_available(i2c))
    tight_loop_contents();
```

That loop has no timeout check. It is reached once per byte before the read
command is queued. With a 32-byte read the TX FIFO (16 entries) can fill, and
a target holding SCL low leaves the firmware stuck there indefinitely. The
documented 100 ms budget is therefore not a hard bound for the SDK read
helper.

The SDK does not generate a STOP on a timeout where the master never reached
an abort: a target that stretches SCL keeps the bus busy. Resetting the block
(`i2c_deinit`) releases the RP2040 pads, but if the target is still holding
SCL low the next START cannot complete.

## What this firmware does instead

`src/i2c_master.c` drives the same registers directly:

1. one 90 ms deadline per transaction, checked at **every** wait, including
   the TX-FIFO-room wait that the SDK leaves unbounded;
2. per-byte completion with `TX_EMPTY`, abort reason captured before
   `clr_tx_abrt`;
3. on timeout: set `IC_ENABLE.ABORT`, wait <=2 ms for `MST_ACTIVITY` to clear,
   park the pads, reset the block, then clock up to nine SCL pulses and a STOP
   (<=3 ms, bounded, no address bytes);
4. on every exit path (success, NAK, bus error, timeout): GP4/GP5 back to SIO
   inputs, pulls disabled, block reset. Counts returned are the bytes actually
   completed, never the requested count.

The 90 + 2 + 3 ms budget keeps a failed command under the 100 ms limit.
`begin_bus()` re-initialises the block for each command, which also clears any
stale `restart_on_next`-equivalent state; this port never leaves the block
enabled between commands.

## Limits of the claim

Compile/link and host tests are real, and ordinary write/read/write-read
transactions were exercised on silicon in the bench session recorded in
[`../../docs/hardware-validation.md`](../../docs/hardware-validation.md).
Abort timing, bus-clear effectiveness against a target that holds SCL low, and
USB CDC latency were **not** measured. A short-count success is always reported
as `ERR BUS`, never as `OK`.
