#!/bin/sh
# Compile and run the sensor firmware's host-side tests.
#
# This is NOT a firmware build: it compiles the SDK-free parser (cmd.c) and
# core (core.c) with the host C compiler. The SDK adapter (main.c /
# i2c_target.c) is covered by scripts/build_firmware.sh.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

CC=${CC:-cc}
OUT="$ROOT/build/host-tests"
mkdir -p "$OUT"

echo "host compiler: $($CC --version | head -1)"

echo "== firmware command parser =="
"$CC" -std=c99 -O1 -Wall -Wextra -Werror \
    -Ifirmware/src \
    firmware/src/cmd.c firmware/host_test/test_cmd.c \
    -o "$OUT/test_cmd"
"$OUT/test_cmd"

echo "== firmware state/trace/lease core =="
"$CC" -std=c99 -O1 -Wall -Wextra -Werror -DCB_CORE_HOST \
    -Ifirmware/src \
    firmware/src/cmd.c firmware/src/core.c firmware/host_test/test_core.c \
    -o "$OUT/test_core"
"$OUT/test_core"
