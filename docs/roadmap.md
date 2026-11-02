# Roadmap

This is a direction document, not a delivery promise. Nothing below is
implemented unless it also appears in [`scope-m1.md`](scope-m1.md). The order is
deliberate: prove each expansion on the bench before adding it to the supported
path.

## Near term — harden the existing bench

1. **Level shifting and supply audit for real targets.** The current bench
   assumes a 3.3 V-only bus. A real target may have 5 V pull-ups or a 5 V rail.
   Add a documented level-shifter stage, a target-rail voltage check and an
   explicit fail-closed pre-flight, rather than relying on the operator to
   notice.
2. **Electrical evidence.** Capture SDA/SCL with a logic analyser during arm,
   transfer, expiry and stuck-bus recovery. Compare the firmware's completed-byte
   accounting with wire-level ACK/NAK and measure release latency at expiry.
3. **Stuck-bus and repeated-start hardening.** Exercise a target that holds SCL
   low, repeated-START write continuations, and multi-master arbitration loss;
   either support them explicitly or keep them declared unsupported.
4. **Vision-assisted observation (future).** A camera can record indicator
   LEDs, displays or physical state during a campaign. It must be an observer,
   not a source of truth: image data would be timestamped and stored beside the
   I2C trace, and no behaviour would be inferred without a human-reviewed
   baseline.

## Proposed expansion — Tigard companion

A [Tigard](https://github.com/tigard-tools/tigard) is an FTDI FT2232H-based
multi-protocol tool with UART, SPI, I2C, JTAG/SWD and a logic-analyser port,
plus directional level shifters covering 1.8–5.5 V. It is proposed as a
**companion controller / debug probe**, not as a replacement
for the impersonator.

What it could contribute:

- an independent debug and programming path (JTAG, SWD, SPI or UART depending
  on the target), so the bench host is not also the only way to recover a
  board;
- selectable voltage levels for Tigard’s own interfaces when talking to a
  compatible target; this is not a generic inline translator for Pico GPIO;
- a logic-analyser tap and a second observation point that does not depend on
  the Pico's USB stack.

What it is **not**:

- **Not a drop-in sensor.** Tigard has a different form factor, pinout,
  electrical envelope and firmware. It cannot be substituted for the
  impersonator board in the current wiring, and this project has no Tigard
  firmware or adapter board.
- **Not a licence to put 5 V on a Pico.** Tigard’s onboard translators serve
  its own interfaces. The Pico needs a separate, protocol-appropriate
  translation circuit, with verified rail sequencing and measurements.
- **Not a built-in logic analyser.** Its analyser connector is a tap for an
  external instrument.

Exit criteria before this moves out of "proposal": a schematic, a power/rail
audit, a documented pin mapping, and bench evidence that the companion can
observe or recover the bus without disturbing it.

## Proposed expansion — ESP32-C5 wireless endpoint

An ESP32-C5 is proposed as a **wireless control/telemetry endpoint**, not as a
drop-in replacement for either Pico.

What it could contribute:

- Wi-Fi control of arm/disarm/collect from a host that is not physically
  tethered to the bench;
- out-of-band status and alerting while the USB host is disconnected;
- a path to isolated or remote campaigns.

Hard constraints:

- **ESP32-C5 GPIO is not 5 V safe.** Use level-compatible signals and check
  the exact board schematic. Development boards may accept 5 V USB power
  through an onboard regulator; that does not make their GPIO 5 V tolerant.
- **No radio work exists here.** There is no ESP32 firmware, no Wi-Fi stack, no
  pairing or credential handling, and no over-the-air update path in this
  repository.
- **Security is a design input.** A wireless arm/disarm path would need
  authentication, replay protection, bounded leases enforced on-device and a
  fail-safe release on link loss. None of that is implemented.

Exit criteria: a separate firmware project, a threat model, a 3.3 V-only
schematic, and bench proof that loss of the wireless link parks the target.

## Longer term — exploratory only

- **SPI and I3C impersonation.** Both need new profile schemas and framing
  designs; register-map concepts do not transfer unchanged.
- **Replayable transaction sessions from raw hardware traces.** Today
  `collect --out` writes a raw campaign. Converting it to the replay schema is
  a separate, unimplemented step.
- **Multi-target campaigns.** More than one impersonated address on one bus,
  with per-address leases and a shared time base.

## Non-goals

- Claiming fidelity to a named commercial sensor without a documented
  characterisation campaign.
- Unsupervised connection to production equipment.
- Power switching, reset control or flashing as part of the test tooling.
