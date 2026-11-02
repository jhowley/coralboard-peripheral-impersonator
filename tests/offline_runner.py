#!/usr/bin/env python3
"""Offline test runner: deny network before any bench import.

Installs a CPython audit hook that raises PermissionError on socket connect /
bind / name resolution, then runs the unittest suite. There is no opt-out env
var: to use the network, run the tests some other way (and say so in the
report).

Nothing here reaches hardware: the serial tests use a local pty pair, and the
controller tests use tests/support/fake_pico.py (a labelled test double).
"""

from __future__ import annotations

import os
import platform
import sys
import unittest

_BLOCKED_EVENTS = frozenset({
    "socket.connect",
    "socket.bind",
    "socket.getaddrinfo",
    "socket.gethostbyname",
    "socket.sendto",
    "urllib.Request",
})


def install_deny_hook() -> None:
    def hook(event, args):
        if event in _BLOCKED_EVENTS:
            raise PermissionError(
                f"network-denied: audit event {event!r} blocked by tests/offline_runner.py"
            )

    sys.addaudithook(hook)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    install_deny_hook()
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    sys.path.insert(0, os.path.join(root, "src"))
    sys.path.insert(0, here)

    print("=" * 72)
    print(f"coralboard-bench offline test run")
    print(f"python      : {platform.python_version()} ({sys.executable})")
    print(f"platform    : {platform.platform()}")
    print(f"root        : {root}")
    print(f"network     : DENIED (audit hook: {sorted(_BLOCKED_EVENTS)})")
    print(f"hardware    : none (pty pair + labelled fake transport)")
    print("=" * 72)

    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=here, pattern="test_*.py", top_level_dir=here)
    verbosity = 2 if "-v" in argv else 1
    result = unittest.TextTestRunner(verbosity=verbosity, buffer=False).run(suite)
    print("-" * 72)
    print(f"offline run: tests={result.testsRun} failures={len(result.failures)} "
          f"errors={len(result.errors)} skipped={len(result.skipped)}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
