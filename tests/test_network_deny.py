"""Guard self-test: this only passes under scripts/run_offline_tests.sh.

If it is run without the network-deny runner the connect would fail with
ConnectionRefused (also an OSError), so the assertion on the message text is
deliberate: it proves the audit hook fired and this suite ran offline.
"""

import socket
import sys
import unittest


class TestNetworkDenied(unittest.TestCase):
    def test_connect_is_denied_by_runner(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        with self.assertRaises(PermissionError) as ctx:
            sock.connect(("127.0.0.1", 9))
        self.assertIn("network-denied", str(ctx.exception))

    def test_getaddrinfo_is_denied_by_runner(self):
        with self.assertRaises(PermissionError) as ctx:
            socket.getaddrinfo("example.com", 80)
        self.assertIn("network-denied", str(ctx.exception))

    def test_bench_modules_never_import_socket(self):
        sys.path.insert(0, __file__.rsplit("/tests/", 1)[0] + "/src")
        import coralbench.serial_link as link
        import coralbench.controller as ctl
        import coralbench.cli as cli
        for mod in (link, ctl, cli):
            self.assertNotIn("socket", vars(mod))


if __name__ == "__main__":
    unittest.main()
