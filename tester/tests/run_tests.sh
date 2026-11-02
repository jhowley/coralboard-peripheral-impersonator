#!/bin/bash
# Build and run the host-side behavioural tests. No hardware, no SDK.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
CC=${CC:-cc}
OUT="$ROOT/build-host"
mkdir -p "$OUT"
set -x
$CC -std=c11 -Wall -Wextra -Werror -O1 -I"$ROOT/src" \
    "$ROOT/src/tester_core.c" "$ROOT/tests/test_core.c" \
    -o "$OUT/test_core"
"$OUT/test_core"
