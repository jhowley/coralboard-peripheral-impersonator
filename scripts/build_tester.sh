#!/bin/sh
# Build the TESTER (I2C master) firmware for the Raspberry Pi Pico / RP2040.
#
# Required environment:
#   PICO_SDK_PATH         path to a pico-sdk checkout (verified with 2.1.1)
# Optional:
#   PICO_TOOLCHAIN_PATH   root of an arm-none-eabi toolchain (else PATH is used)
#   BUILD_DIR             output directory (default: build/firmware-tester)
#
# Produces coralboard_tester.uf2. Flashing is a separate, deliberate operator
# step and is not performed by this script.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

: "${PICO_SDK_PATH:?set PICO_SDK_PATH to a pico-sdk checkout (2.1.1 verified)}"
command -v cmake >/dev/null 2>&1 || { echo "error: cmake not found in PATH" >&2; exit 1; }

if [ ! -f "$PICO_SDK_PATH/external/pico_sdk_import.cmake" ]; then
    echo "error: $PICO_SDK_PATH does not look like a pico-sdk checkout" >&2
    exit 1
fi

if [ -n "${PICO_TOOLCHAIN_PATH:-}" ]; then
    export PICO_TOOLCHAIN_PATH
    PATH="$PICO_TOOLCHAIN_PATH/bin:$PATH"
    export PATH
fi
command -v arm-none-eabi-gcc >/dev/null 2>&1 || {
    echo "error: arm-none-eabi-gcc not found; set PICO_TOOLCHAIN_PATH or PATH" >&2
    exit 1
}

BUILD_DIR=${BUILD_DIR:-$ROOT/build/firmware-tester}
cmake -S "$ROOT/tester" -B "$BUILD_DIR" \
      -DPICO_BOARD=pico \
      -DCMAKE_BUILD_TYPE=Release \
      -DPICO_SDK_PATH="$PICO_SDK_PATH"
cmake --build "$BUILD_DIR" --parallel

ls -l "$BUILD_DIR/coralboard_tester.uf2"
echo "OK: $BUILD_DIR/coralboard_tester.uf2"
