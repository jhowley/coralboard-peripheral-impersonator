# Third-party notices

This repository's own code is MIT-licensed (see `LICENSE`). It builds against,
but does not vendor, the following third-party components.

## Raspberry Pi Pico SDK

- Project: https://github.com/raspberrypi/pico-sdk
- Version used for the verified build: **2.1.1**, commit
  `bddd20f928ce76142793bef434d4f75f4af6e433`
- Licence: BSD-3-Clause (see `LICENSE.TXT` in the SDK checkout)
- Use here: headers, startup, USB CDC stdio, I2C peripheral support and build
  system. The SDK is a build input supplied by the builder, not copied into
  this repository.

## vmilea/pico_i2c_slave

- Project: https://github.com/vmilea/pico_i2c_slave
- Revision used for the verified build: commit
  `a25d1040950cdb4d37b50e05886c6195307f245c`
- Licence: MIT, Copyright (c) 2021 Valentin Milea
  <valentin.milea@gmail.com> (see `LICENSES/MIT.txt` in that checkout)
- Use here: I2C target-mode handler in the sensor firmware. Supplied as a build
  input via `PICO_I2C_SLAVE_PATH`, not copied into this repository.

## Toolchain

`arm-none-eabi-gcc` 13.2.1 (GNU Arm Embedded Toolchain) is used to cross-compile
the firmware. It is supplied by the builder and is not redistributed here.

No third-party source is modified by this repository. The firmware's use of
`pico_i2c_slave` is through its public header and documented behaviour only.
