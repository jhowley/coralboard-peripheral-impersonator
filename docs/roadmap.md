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

### Integration path (assessed against the live board)

Tigard is an FTDI FT2232H, so from the Coralboard it is an ordinary USB device
(`0403:6010`). Driving its UART/SPI/I2C/JTAG happens over USB, and the clean
route on this image is **userspace MPSSE via `pyftdi` over `libusb`**, not the
kernel `ftdi_sio`/`usbserial` modules:

- The Yocto image ships **no USB-serial kernel modules** (no `ftdi_sio`, no
  `usbserial`, no `cdc_acm` — confirmed empty on the live board), so the kernel
  will not grab Tigard's interfaces. That is an advantage, not a gap: `pyftdi`
  gets exclusive MPSSE access without ever detaching a driver — the same
  fail-closed behaviour `usb_link.py` already enforces for the Pico.
- `libusb-1.0.so.0` **is** present, and `pyftdi` (plus its `pyserial`
  dependency) is pure Python, so it installs into a `/home` virtual
  environment with no compiler and no root-filesystem change.
- This keeps Tigard a **seamless intermediary**: the Coralboard talks plain USB
  to the Tigard, and the Tigard presents the target side at a selected 1.8–5.5 V,
  moving voltage translation off the Pico entirely.

```text
Coralboard ──USB──► Tigard (FT2232H) ──UART / SPI / I2C / JTAG @ 1.8–5.5 V──► target
  host + audit app        MPSSE engine             selected logic level
```

The audit transport layer would therefore have two interchangeable front ends
behind one interface: **libusb-direct** (the Pico, already proven) and
**pyftdi/MPSSE** (the Tigard). Tier-1 enumeration sees the Tigard itself as a
device and the target through its buses, so a Tigard is both an object to
fingerprint and a conduit to the device under test.

Not yet verified on hardware: that a physical Tigard enumerates cleanly at
`0403:6010` on this USB stack, and MPSSE timing on this aarch64 kernel. That is
the one bench test needed before this moves out of "proposal".

Exit criteria before this moves out of "proposal": a schematic, a power/rail
audit, a documented pin mapping, a `/home`-venv `pyftdi` import proof, and bench
evidence that the Tigard enumerates and drives the bus without disturbing the
Pico front end.

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

## Proposed expansion — tiered device audit

Plug an unfamiliar USB peripheral, camera, or device into the Coralboard's
USB-A host port (or attach it to the Pico/Tigard front end) and let the host
**enumerate → characterize → fuzz → extract → adapt**, recording every probe
and its effect. Each tier feeds the next, and the loop pauses at every hazard
rather than running unattended into a brick.

| Tier | What it learns | Risk | Automatable? |
| --- | --- | --- | --- |
| 1. Enumerate | VID/PID, strings, serial, interfaces, endpoints, class codes, HID report descriptors, mass-storage probe (read-only), I²C address scan | very low | yes |
| 2. Characterize | standard/vendor command set, reply shapes, how it answers | low | mostly, with timeouts |
| 3. Fuzz / stress | crashes, hangs, parsing weaknesses | medium | yes, **with a power-cycle watchdog** |
| 4. Firmware extract + analyse | actual code, strings, known CVEs, hardcoded keys | medium–high | extraction scriptable, interpretation not |
| 5. Adapt | "given 1–4, probe here next" | varies | only inside a human-set envelope |

The closed loop with **physical observation** is the part this project can
offer that is not off-the-shelf: the camera/NPU watches LEDs, screens and
physical state during each probe, so a crash, a reboot or an unexpected light
pattern becomes a recorded data point — and the next round of probes is chosen
from what was actually observed, not just from bus replies.

### Implementation safety (assessed against the live board)

Tier 1 (and most of tier 2) can be added **without touching any existing
software**. Assessed 2026-11-03 against the running board:

- **New code lives in a new `/home` directory** (e.g. `/home/coralboard-audit`),
  alongside — never inside — the existing `/home/coralboard-bench` and
  `/home/coralboard-tester` trees.
- **No root-filesystem writes.** `/` is 98 % full (33.6 MB free); `/home` has
  46.8 GB free. Any Python dependency goes in a `/home` virtual environment,
  not `pip install` into `/usr`.
- **No new driver or kernel change.** USB enumeration reads
  `/sys/bus/usb/devices` and `lsusb`, both already present and working. The
  userspace `libusb-1.0.so.0` path already proven by `usb_link.py` (via
  `ctypes`) is reused — `pyusb` is not installed and is not required.
- **No Pico firmware change, no M52 touch.** The Picos and the
  `rpmsg_ctrl`/M52 path are untouched; enumeration is passive host-side USB.
- **No network change.** The Wi-Fi link and ADB path are left exactly as they
  are.

The one shared resource is the single USB-A host port: an audit target is
plugged into the same hub as the bench Picos, which is a physical coexistence,
not a software conflict — enumeration does not claim or reconfigure anything.

Deeper tiers are a different cost class and are gated separately: they need
aarch64-compiled tooling or a `/home` venv, physical wiring for SPI/JTAG, and a
safety watchdog with a known-good rollback before fuzzing can run unattended.

### Exit criteria for tier 1

- A separate `coralboard-audit` module that enumerates a plugged device and
  emits a fingerprint (descriptors, classes, strings, endpoints) without
  claiming any interface or detaching a driver.
- Evidence that enumeration changes nothing else on the bus: the bench Picos
  still enumerate, and a before/after `lsusb`/sysfs snapshot is identical
  except for the newly plugged device.
- The fingerprint is recorded through the existing evidence backbone so a
  probe and its result are replayable.

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
