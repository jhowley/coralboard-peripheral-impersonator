# TESTER protocol (USB CDC, ASCII lines)

Transport is identical to the sensor firmware: default Pico SDK USB CDC
descriptor, `2e8a:000a`, interface 0 CDC-ACM + interface 1 CDC-Data with
bulk OUT `0x02` / bulk IN `0x82` (64-byte packets). One command per `\n`
line, CR ignored, replies one per line. Lines are bounded to 256 bytes;
longer input is answered `ERR TOO_LONG` once the newline arrives.

Every reply names the firmware version as its final field:

```
... fw=1.0.0
```

PING and STATUS additionally carry the role `TESTER`. A client must verify a
PING reply of the exact form `OK PING TESTER fw=1.0.0` before sending any bus
command; a sensor firmware would not answer that line.

## Commands (complete set)

| Command | Meaning |
| --- | --- |
| `PING` | identity/version, no bus activity |
| `STATUS` | counters and fixed configuration, no bus activity |
| `W <hex>` | one write transaction, 1..32 bytes, fixed address 0x48 |
| `R <n>` | one read transaction, 1..32 bytes, fixed address 0x48 |
| `WR <hex> <n>` | write bytes, then repeated START, read n bytes |

`<hex>` is 2..64 hex digits (1..32 bytes), even length, no `0x` prefix.
`<n>` is decimal 1..32. There is no address argument, no scan and no
register-pointer argument: the payload is whatever the caller supplies.

## Replies

```
OK PING TESTER fw=1.0.0
OK STATUS role=TESTER fw=1.0.0 addr=0x48 i2c=0 sda=4 scl=5 hz=100000 \
    pins=input pulls=off ops=0 ok=0 err=0 timeouts=0 last=none
OK W n=<count> fw=1.0.0
OK R n=<count> data=<hex> fw=1.0.0
OK WR wn=<count> rn=<count> data=<hex> fw=1.0.0
ERR BAD_COMMAND fw=1.0.0
ERR BAD_HEX fw=1.0.0
ERR BAD_VALUE fw=1.0.0
ERR TOO_LONG fw=1.0.0
ERR NAK phase=w at=<n> fw=1.0.0
ERR NAK phase=r n=<n> fw=1.0.0
ERR TIMEOUT phase=w at=<n> fw=1.0.0
ERR TIMEOUT phase=r n=<n> fw=1.0.0
ERR BUS phase=w at=<n> fw=1.0.0
ERR BUS phase=r n=<n> fw=1.0.0
ERR BUS fw=1.0.0
```

Counts are the bytes the hardware actually moved, not the bytes requested.
On `phase=w`, `at=<n>` is the number of data bytes acknowledged before the
failing byte (0 can also mean the address phase itself was NACKed). On
`phase=r`, `n=<n>` is the number of bytes received before the failure.
`data=` appears only in `OK` replies.

`ERR BUS` with no phase means the port reported success with a count that did
not match the request — a firmware bug, surfaced rather than hidden.

## Limits and timing

- One bus transaction per command. No transaction is started at boot.
- Fixed address `0x48`, I2C0, GP4=SDA, GP5=SCL, 100 kHz.
- Transfer budget 90 ms; abort wait <=2 ms; bus-clear recovery <=3 ms, so a
  failing command returns in under 100 ms.
- After every transaction — success, NAK, error or timeout — the I2C block is
  reset and GP4/GP5 return to SIO inputs with pulls disabled.
- On timeout only, the master is aborted and up to nine SCL pulses plus a STOP
  are clocked as bounded recovery. No address is sent during recovery.

## STATUS fields

`ops` = bus commands attempted, `ok` = completed transactions, `err` = failed
transactions, `timeouts` = subset of `err`, `last` = `none|ok|nak|timeout|bus`.
`pins=input pulls=off` is the resting state; STATUS is only serviced between
commands, so it always describes the parked state.
