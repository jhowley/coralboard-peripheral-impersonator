"""Compare the firmware core C harness with the Python simulator.

The two implementations are not assumed identical: the C core is compiled with
the host gcc and its segment transcript is compared byte-for-byte with the
Python simulator for the same script.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from coralbench import profile as P  # noqa: E402
from coralbench import simulator as S  # noqa: E402

GOOD = os.path.join(ROOT, "profiles", "bench-temp-raw-v1.json")


def build_harness(tmp):
    cc = shutil.which(os.environ.get("CC", "gcc"))
    if cc is None:
        raise unittest.SkipTest("host gcc is not available")
    out = os.path.join(tmp, "test_core")
    cmd = [cc, "-std=c99", "-O1", "-Wall", "-Wextra", "-Werror",
           "-DCB_CORE_HOST", "-I", os.path.join(ROOT, "firmware", "src"),
           os.path.join(ROOT, "firmware", "src", "cmd.c"),
           os.path.join(ROOT, "firmware", "src", "core.c"),
           os.path.join(ROOT, "firmware", "host_test", "test_core.c"),
           "-o", out]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return out


def python_transcript(profile):
    target = S.SimulatedTarget(profile, boot_nonce=0xBEEF)
    target.reload(profile)  # LOAD: epoch counter 1
    target.arm()            # ARM:  epoch counter 2
    target.write(b"\x00")
    target.read(1)
    target.write(b"\x01")
    target.read(2)
    target.write(b"\x02\xaa")
    target.read(1)
    target.write(b"\x02")
    target.read(6)  # crosses pointer_wrap=4 every byte
    target.disarm()
    lines = ["C {:08x} {:08x}".format(target.cfg, target.epoch)]
    for ev in target.drain():
        lines.append("E {:08x} {} {} {} {} {} {}".format(
            ev.epoch, ev.seq, ev.op, ev.ptr, ev.length,
            1 if ev.truncated else 0, ev.data.hex()))
    lines.append("D {} {} {} {} complete={}".format(
        target.emitted, target.dropped, target.aborted, target.drained,
        1 if target.complete_candidate else 0))
    return lines


class TestCoreParity(unittest.TestCase):
    def test_c_core_matches_python_simulator(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = build_harness(tmp)
            proc = subprocess.run([harness, "--transcript"], check=True,
                                  capture_output=True, text=True)
        c_lines = [line for line in proc.stdout.splitlines() if line.strip()]
        py_lines = python_transcript(P.load_profile(GOOD))
        self.assertEqual(c_lines, py_lines)


if __name__ == "__main__":
    unittest.main()
