# Contributing

Thanks for looking. This is an experimental bench project; the most useful
contributions are reproducible tests, documentation corrections and
carefully-scoped hardware evidence.

## Before you start

- Read [`docs/scope-m1.md`](docs/scope-m1.md) and
  [`docs/hardware-validation.md`](docs/hardware-validation.md). Do not describe
  a capability as working unless it is in the verified column.
- The profiles are **bounded synthetic register maps**, not device models.
  Do not add a profile that claims fidelity to a named part or sets
  `claimed_device` to anything but `null`.
- Do not include board serial numbers, LAN addresses, host names, credentials,
  raw captures or build artifacts in commits.

## Development workflow

```sh
make test          # Python + host C + TESTER host + TESTER client, offline
make check         # guards against local paths, serials, LAN addresses, artifacts
```

`make test` must pass without a Pico, an SDK, network access or root. If you
change behaviour, add a test that fails before the change. The Python suite
runs under an audit hook that denies sockets; tests that need a serial port use
a local pty pair, and USB tests use the labelled fake backend.

Firmware changes need the host-side parser/core tests plus a cross-build:

```sh
export PICO_SDK_PATH=/path/to/pico-sdk                 # 2.1.1 verified
export PICO_I2C_SLAVE_PATH=/path/to/pico_i2c_slave     # a25d1040 verified
export PICO_TOOLCHAIN_PATH=/path/to/arm-none-eabi      # optional if on PATH
make build-sensor
make build-tester
```

Keep the build free of warnings: the firmware is compiled with
`-Wall -Wextra -Werror`.

## What CI does and does not do

CI runs the offline suites only. It has **no hardware**, no SDK download and no
network during tests. A green CI run means the host-side contracts and the
SDK-free firmware logic pass; it does not mean a board was tested. Hardware
results belong in `docs/hardware-validation.md` with the build hash, wiring,
observed operations and explicit non-claims.

## Pull requests

- One focused change per PR.
- State what you ran and the exact output summary.
- Call out anything you could not test, especially anything requiring a board.
- Do not reformat unrelated files or rewrite production sources for style.
- By contributing, you agree your contribution is licensed under the
  repository's MIT licence.

## Safety

Never propose wiring that puts 5 V on a Pico pin, joins 3V3 rails, or connects
a target without a common ground and a voltage check. The generator/controller
tooling has no power or reset control; do not add one without a separate
safety review.
