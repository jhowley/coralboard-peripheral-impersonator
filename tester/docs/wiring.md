# Wiring

The canonical wiring diagram, forbidden connections and pre-flight checklist
live in [`../../docs/wiring.md`](../../docs/wiring.md). Both firmwares use the
same two-Pico bus described there.

TESTER-specific notes:

- The TESTER is the **only** I2C master. It drives SDA/SCL only during an
  explicit `W`, `R` or `WR` command, and parks both pins as inputs with pulls
  disabled after every transaction.
- Both 5.1 kΩ pull-ups go to the TESTER's `3V3(OUT)` pin 36. Do not use the
  SENSOR's 3V3 pin as a second reference and do not join the 3V3 rails.
- Each board is powered from its own USB cable. Never leave one board
  unpowered while the other drives the bus.
- Before any bus command, verify `OK PING TESTER fw=1.0.0` and a `STATUS` line
  with no bus activity. Identity is asserted by the PING reply, not by the USB
  descriptor.
- There is no scan, no address argument and no boot-time transaction. The
  address is fixed at `0x48`, I2C0, 100 kHz, 1–32 bytes per command.
