#!/bin/sh
# Guard against publishing local paths, board serials, LAN addresses or
# build/evidence artifacts. Run from the repository root by CI.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

failed=0

check_no_match() {
    label=$1
    pattern=$2
    # Exclude this script (it contains the patterns by definition).
    hits=$(grep -RInE "$pattern" . \
        --exclude-dir=.git \
        --exclude-dir=build \
        --exclude-dir=__pycache__ \
        --exclude=check_public_tree.sh 2>/dev/null || true)
    if [ -n "$hits" ]; then
        echo "FAIL: $label" >&2
        echo "$hits" >&2
        failed=1
    fi
}

check_no_match "absolute home paths" '(/root/|/home/[A-Za-z0-9._-]+/)'
check_no_match "private LAN addresses" '192\.168\.[0-9]+\.[0-9]+'
check_no_match "observed board serial prefix" 'E6614C[0-9A-Fa-f]{10}'
check_no_match "ssh invocation" 'root@[0-9]'

artifacts=$(find . -path ./.git -prune -o -path ./build -prune \
    -o -path ./tester/build -prune -o -path ./tester/build-host -prune \
    -o -path '*/__pycache__' -prune -o -type f \
    \( -name '*.uf2' -o -name '*.elf' -o -name '*.bin' -o -name '*.jsonl' \
       -o -name '*.log' -o -name '*.pyc' \) -print 2>/dev/null || true)
if [ -n "$artifacts" ]; then
    echo "FAIL: build/evidence artifacts present" >&2
    echo "$artifacts" >&2
    failed=1
fi

if [ "$failed" -ne 0 ]; then
    echo "public-tree check: FAILED" >&2
    exit 1
fi
echo "public-tree check: OK"
