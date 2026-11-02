import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from coralbench import profile as P  # noqa: E402

GOOD = os.path.join(ROOT, "profiles", "bench-temp-raw-v1.json")
ALT = os.path.join(ROOT, "profiles", "bench-temp-alt-v1.json")


def good_doc():
    with open(GOOD, encoding="utf-8") as fh:
        return json.load(fh)


class TestLoad(unittest.TestCase):
    def test_load_good_profile(self):
        p = P.load_profile(GOOD)
        self.assertEqual(p.profile_id, "bench-temp-raw-v1")
        self.assertEqual(p.address, 0x48)
        self.assertEqual(p.register_space, 256)
        self.assertEqual(len(p.image), 256)
        self.assertEqual(p.image[0], 0x19)
        self.assertEqual(p.writable, frozenset({1, 2, 3}))
        self.assertTrue(p.auto_increment)
        self.assertEqual(p.pointer_wrap, 4)
        self.assertEqual(p.pointer_initial, 0)
        self.assertEqual(p.lease_max_ms, 10000)
        self.assertEqual(p.fidelity, "synthetic-bounded")
        self.assertEqual(len(p.digest), 64)
        self.assertIn("bench-temp-raw-v1", p.describe())

    def test_second_profile_is_interchangeable(self):
        p = P.load_profile(ALT)
        self.assertEqual(p.profile_id, "bench-temp-alt-v1")
        self.assertFalse(p.auto_increment)
        self.assertEqual(p.lease_max_ms, 5000)
        self.assertEqual(p.writable, frozenset({2, 3}))
        self.assertNotEqual(p.image, P.load_profile(GOOD).image)


class TestValidation(unittest.TestCase):
    def assertCode(self, doc, code):
        with self.assertRaises(P.ProfileError) as ctx:
            P.validate_profile(doc)
        self.assertIn(code, ctx.exception.codes(), f"issues={ctx.exception.codes()}")

    def test_missing_field(self):
        doc = good_doc()
        del doc["read_policy"]
        self.assertCode(doc, "missing_field")

    def test_fidelity_claim_rejected(self):
        doc = good_doc()
        doc["fidelity"] = "faithful-tmp102"
        self.assertCode(doc, "fidelity")

    def test_claimed_device_rejected(self):
        doc = good_doc()
        doc["claimed_device"] = "TMP102"
        self.assertCode(doc, "claimed_device")

    def test_wrong_address(self):
        doc = good_doc()
        doc["address"] = 0x4A
        self.assertCode(doc, "address")

    def test_register_space_bounded(self):
        doc = good_doc()
        doc["register_space"] = 4096
        self.assertCode(doc, "register_space")

    def test_image_length(self):
        doc = good_doc()
        doc["defaults"]["image_hex"] = doc["defaults"]["image_hex"][:100]
        self.assertCode(doc, "image_length")

    def test_image_non_hex(self):
        doc = good_doc()
        doc["defaults"]["image_hex"] = "zz" * 256
        self.assertCode(doc, "image_hex")

    def test_writable_out_of_range(self):
        doc = good_doc()
        doc["writable"] = [1, 300]
        self.assertCode(doc, "writable_range")

    def test_lease_bound(self):
        doc = good_doc()
        doc["lease_max_ms"] = 60000
        self.assertCode(doc, "lease_bound")

    def test_pointer_wrap_range(self):
        doc = good_doc()
        doc["read_policy"]["pointer_wrap"] = 999
        self.assertCode(doc, "pointer_wrap")

    def test_pointer_initial_must_be_inside_wrap(self):
        doc = good_doc()
        doc["pointer_register"]["initial"] = 4
        self.assertCode(doc, "pointer_register")

    def test_pointer_initial_is_used(self):
        doc = good_doc()
        doc["pointer_register"]["initial"] = 2
        self.assertEqual(P.validate_profile(doc).pointer_initial, 2)


class TestConfigCrc(unittest.TestCase):
    def test_config_crc_binds_full_configuration(self):
        base = good_doc()
        crc = P.config_crc(P.validate_profile(base))
        wmask_changed = good_doc()
        wmask_changed["writable"] = [1, 2]
        self.assertNotEqual(crc, P.config_crc(P.validate_profile(wmask_changed)))
        init_changed = good_doc()
        init_changed["pointer_register"]["initial"] = 2
        self.assertNotEqual(crc, P.config_crc(P.validate_profile(init_changed)))
        flags_changed = good_doc()
        flags_changed["read_policy"]["auto_increment"] = False
        self.assertNotEqual(crc, P.config_crc(P.validate_profile(flags_changed)))


class TestDigest(unittest.TestCase):
    def test_digest_is_order_independent(self):
        a = good_doc()
        b = json.loads(json.dumps(a))
        b = {k: b[k] for k in reversed(list(b))}
        self.assertEqual(P.validate_profile(a).digest, P.validate_profile(b).digest)

    def test_digest_changes_with_content(self):
        a = good_doc()
        b = json.loads(json.dumps(a))
        b["defaults"]["image_hex"] = "00" * 256
        self.assertNotEqual(P.validate_profile(a).digest, P.validate_profile(b).digest)


if __name__ == "__main__":
    unittest.main()
