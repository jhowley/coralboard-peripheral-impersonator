# Wire protocol (Pico USB CDC, ASCII lines)

The protocol below is transport-independent. On a kernel with CDC ACM it runs
over `/dev/ttyACM*`; on a host with only libusb and no `cdc_acm`, it runs over
the same CDC data endpoints through `coralbench usb --serial <iSerialNumber>`.
The USB transport sets line coding 115200 8N1 and asserts DTR/RTS, then writes
commands to bulk OUT `0x02` and reads replies from bulk IN `0x82`. Framing is
identical: one ASCII command per `\n`, one reply line per `\n`.

One command per line, `\n` terminated, CR ignored, lines bounded to 1024 bytes.
Replies are `OK ...` or `ERR <CODE>`. Every reply is produced from the
firmware's main loop, never from the I2C interrupt.

## Commands

| Command | Meaning |
| --- | --- |
| `PING` | liveness + firmware version |
| `STATUS` | full state snapshot |
| `LOAD crc=<8 hex> flags=<2 hex> wrap=<1..256> init=<0..255> tag=<1..32> wmask=<64 hex> image=<512 hex>` | replace the register template; **only while disarmed and only after the previous trace is fully drained** |
| `ARM lease_ms=<1..10000>` | enable the I2C target for a bounded lease; starts a new epoch; the expiry alarm is scheduled before the bus is enabled |
| `DISARM` | disable the target, seal the trace (idempotent) |
| `TRACE DRAIN n=<1..64>` | stream up to `n` trace segments; allowed while armed |
| `TRACE END` | completeness verdict for the current epoch |

`LOAD` fields: `crc` = CRC-32 (zlib) of the 256-byte image; `flags` bit 0 =
post-read auto-increment; `wrap` = pointer modulus; `init` = pointer value at
`ARM` (must be `< wrap`); `tag` = profile id; `wmask` = 32 bytes, one bit per
writable register (bit `reg&7` of byte `reg>>3`). `LOAD` while armed is
`ERR ARM_ACTIVE`; `LOAD` with undrained events is `ERR TRACE_PENDING`.

## Replies

```
OK PING m1-i2c fw=0.2.0
OK STATUS armed=<0|1> sealed=<0|1> loaded=<0|1> expired=<0|1> addr=0x48
          profile=<tag> crc=<8 hex> cfg=<8 hex> epoch=<8 hex> boot=<4 hex>
          lease_left_ms=<n> emitted=<n> dropped=<n> aborted=<n> drained=<n>
OK LOAD crc=<8 hex> len=256 flags=<2 hex> wrap=<n> init=<n> cfg=<8 hex> epoch=<8 hex>
OK ARM addr=0x48 lease_ms=<n> cfg=<8 hex> epoch=<8 hex>
OK DISARM sealed=1 emitted=<n> dropped=<n> aborted=<n> drained=<n> epoch=<8 hex>
T epoch=<8 hex> seq=<n> op=<r|w> ptr=<n> len=<n> data=<hex> trunc=<0|1>
OK DRAIN n=<drained> emitted=<n> dropped=<n> aborted=<n> sealed=<0|1>
         epoch=<8 hex> drained=<n>
OK TRACEEND emitted=<n> dropped=<n> aborted=<n> sealed=<0|1> complete=<0|1>
         epoch=<8 hex> cfg=<8 hex> drained=<n>
ERR <CODE>
```

Error codes: `BAD_COMMAND`, `BAD_VALUE`, `BAD_IMAGE`, `CRC`, `ARM_ACTIVE`,
`NOT_LOADED`, `LEASE_TOO_LONG`, `TRACE_PENDING`, `TIMER`. `ERR TIMER` means
the expiry alarm could not be scheduled (SDK returned a non-positive id) or
the lease expired before the bus could be enabled; the target is sealed and
detached, never left active. Unknown keys, duplicate keys and unknown verbs are
`BAD_COMMAND` — the firmware refuses rather than guessing.

## Identity: epoch and configuration hash

- `boot` is a 16-bit boot nonce. `epoch = (boot << 16) | counter`, where the
  counter increments on every successful `LOAD` and every successful `ARM`.
- Every `T` record carries `epoch`. `seq` (32-bit) starts at 0 on `ARM` and
  increments per stored segment; a qualifying campaign is exactly
  `0 .. emitted-1`.
- `cfg` is the CRC-32 of `tag || NUL || image[256] || wmask[32] || flags ||
  wrap_le16 || init`. It binds evidence to the full loaded configuration, not
  just the bytes.
- A `LOAD` clears the trace counters and starts a new epoch; it never relabels
  old events. A new `ARM` restores the image from the loaded template, applies
  `init`, resets transfer/sequence/counters and starts another epoch.

## Post-conditions the host relies on

- `ARM` while armed → `ERR ARM_ACTIVE`. Re-arm requires `DISARM` or expiry.
- `LOAD` while armed → `ERR ARM_ACTIVE`; `LOAD` with undrained events →
  `ERR TRACE_PENDING`.
- `TRACE DRAIN` while armed is allowed and reports `sealed=0`. Streaming is
  observation, not completion.
- `TRACEEND complete=1` only when loaded, sealed, unarmed, `dropped==0`,
  `aborted==0`, the ring is empty, `emitted>0` and `drained==emitted`.
- At boot: unarmed, `loaded=0`, pins high-impedance.
- After `DISARM` or lease expiry: target deinitialised, GP4/GP5 function-NULL
  inputs with pulls disabled.

## Trace semantics

The target library (`vmilea/pico_i2c_slave`, checked against its
`i2c_slave/i2c_slave.c`) raises `I2C_SLAVE_FINISH` on
STOP_DET, START_DET (repeated START) **and TX_ABRT**. Therefore:

| Situation | Behaviour |
| --- | --- |
| `ARM` | image restored from the template; pointer = `init`; new epoch |
| One `T` record | one bus segment: address phase through the next STOP **or** repeated START |
| Master write, first byte of a write segment | pointer = `byte % wrap` |
| Master write, later bytes | stored only if `wmask` allows the register; otherwise ignored |
| Pointer advance on write | `(pointer + 1) % wrap` per data byte |
| Master read | bytes from the pointer upward, wrapping **per byte** at `wrap`: the slave must supply one byte for every `RD_REQ`, so a read that reaches the modulus continues at register 0 of the bounded window |
| Pointer advance on read | only when `flags` bit 0 is set: `(pointer + 1) % wrap` per byte; with auto-increment off the same register is repeated for every requested byte |
| `write(pointer) Sr read(n)` | **two** segments: a write event with `len=0`, then a read event |
| `write(pointer) STOP; write(data) STOP` | two write segments; the second segment's first byte is again a pointer by this fixture's rule |
| `write(pointer) Sr write(data)` | **unsupported**: the library does not distinguish STOP from repeated START, so the second segment's first byte would be read as a pointer. Masters must use STOP between the pointer segment and data writes. |

`T.len` is the segment's **data** byte count (16-bit; a 256-byte read is
legal). For a write segment the first byte is the pointer register and is
**not** included in `len` or `data` (a pointer-only write records `len=0`); for
a read segment it is the number of bytes the target supplied. `T.data` is the
first 8 bytes; `trunc=1` when `len > 8`. A truncated segment cannot qualify a
byte-complete campaign. `seq` is a 32-bit per-epoch counter starting at 0.

The trace records **bytes prepared by the target in each segment**. It is not a
wire sniffer: there is no ACK/NAK field, no arbitration-loss observation and no
proof that the master accepted a byte. A read the master NACKs early is still
recorded at the requested length; a write outside `wmask` is still recorded as
received even though it did not change the image.

The simulator implements the same segment table. `tests/test_core_parity.py`
compiles the firmware core with the host compiler and compares its transcript
with the Python simulator for the same segment script.
