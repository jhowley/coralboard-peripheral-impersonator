#!/bin/sh
# Offline verification: no hardware, no network, no SDK required.
#
# Runs the Python bench suite (network denied by an audit hook), the
# host-compiled firmware parser/core tests, the TESTER host tests and the
# TESTER client tests. Exit status is non-zero on the first failure.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
export PYTHONDONTWRITEBYTECODE=1

echo "== bench Python suite (offline, network denied) =="
python3 tests/offline_runner.py

echo
echo "== sensor firmware host tests (gcc, SDK-free sources) =="
sh scripts/build_firmware_host_tests.sh

echo
echo "== TESTER host tests =="
sh tester/tests/run_tests.sh

echo
echo "== TESTER client offline tests =="
python3 tester/client/test_client_offline.py

echo
echo "offline test run: OK"
