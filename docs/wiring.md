# Wiring — two Pico boards, 3.3 V only, separate USB supplies

This is the wiring used for the bench validation in
[`hardware-validation.md`](hardware-validation.md). It is a two-board I2C bus:

- **SENSOR / impersonator** runs the `coralboard_bench` firmware and answers at
  address `0x48` only while a lease is armed.
- **TESTER** runs the `coralboard_tester` firmware and is the only I2C master.

No real target device is on this bus. Connecting the impersonator in place of a
real device is a separate, supervised step.

## Signals

| Signal | TESTER (master) | SENSOR (target) | Notes |
| --- | --- | --- | --- |
| SDA | GP4, physical pin 6 | GP4, physical pin 6 | one wire |
| SCL | GP5, physical pin 7 | GP5, physical pin 7 | one wire |
| GND | GND, physical pin 8 | GND, physical pin 8 | common ground, first on / last off |
| SDA pull-up | 5.1 kΩ to TESTER 3V3(OUT), **pin 36** | — | one resistor |
| SCL pull-up | 5.1 kΩ to TESTER 3V3(OUT), **pin 36** | — | one resistor |

That is **three wires and exactly two resistors**. Both resistors go to the
same 3.3 V source: the TESTER's `3V3(OUT)` pin 36. Do not add a third pull-up
and do not use the SENSOR's 3V3 pin as a second source.

## Text schematic

```
        TESTER (master)                         SENSOR (impersonator, 0x48)
        +---------------+                       +---------------+
        | GP4  pin 6    +------- SDA -----------+ GP4  pin 6    |
        | GP5  pin 7    +------- SCL -----------+ GP5  pin 7    |
        | GND  pin 8    +------- GND -----------+ GND  pin 8    |
        | 3V3(OUT) pin36|                       |               |
        +---+-----------+                       +---------------+
            |
            +-- 5.1 kohm -- SDA
            +-- 5.1 kohm -- SCL

        Each board is powered from its own USB cable. Only SDA, SCL and GND
        are shared. Pin 36 is used only as the pull-up reference; it is not
        wired to the other board.
```

## Non-negotiable rules

1. **3.3 V logic only.** Pico RP2040 I/O is not 5 V tolerant. No 5 V pull-up,
   no 5 V target, no "it worked for a second" experiments.
2. **Two supplies, one ground.** Power each Pico from its own USB cable.
   Never join the two `3V3(OUT)` pins, and never connect `VBUS`, `VSYS`,
   `3V3_EN`, `RUN` or any other power/control pin between the boards.
3. **Common ground is mandatory.** Join GND first and disconnect it last.
4. **Do not use the SENSOR's 3V3 pin for the pull-ups.** One 3.3 V reference
   only: TESTER pin 36.
5. **Check the target before joining a real device.** If a target board has
   its own pull-ups to a possibly 5 V rail, confirm that rail is 3.3 V before
   SDA/SCL touch it.
6. **Wire with both boards unplugged.** Inspect for bridges and check
   continuity/voltage with a meter before applying power.
7. **The SENSOR must stay high-impedance until armed.** Its firmware leaves
   GP4/GP5 as inputs with pulls disabled at boot, after `DISARM` and after
   lease expiry. Verify this with a meter before trusting a campaign.

## Pre-flight checklist

- [ ] Both boards unplugged while wiring.
- [ ] GND continuity between the boards.
- [ ] SDA and SCL idle high (~3.3 V) with the two 5.1 kΩ resistors, both
      boards powered.
- [ ] No connection between the boards' 3V3, VBUS or VSYS pins.
- [ ] TESTER answers `OK PING TESTER fw=1.0.0` and `STATUS` with no bus
      command.
- [ ] SENSOR answers `OK PING m1-i2c fw=0.2.0` and reports `armed=0`.
- [ ] Only then run one explicit bus command with a short lease
      (`arm --lease-ms 2000`).

The tooling has no power or reset control. Use the bench supply and the
BOOTSEL button deliberately.
