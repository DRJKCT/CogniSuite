"""
Tests for passphrase_recovery.py (pure logic only -- the Qt dialogs in
passphrase_recovery_dialogs.py aren't covered here, since they need a
running Qt application to instantiate).
"""
import os
import sys
import stat
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from passphrase_recovery import (
    estimate_strength,
    meets_minimum,
    generate_recovery_passphrase,
    escrow_path_for,
    escrow_exists,
    write_recovery_escrow,
    read_recovery_escrow,
    delete_recovery_escrow,
)


class TestEstimateStrength(unittest.TestCase):
    def test_empty_is_weak(self):
        level, _ = estimate_strength("")
        self.assertEqual(level, "weak")

    def test_common_weak_passphrase_is_weak_regardless_of_case(self):
        for p in ["password", "PASSWORD", "Password123", "letmein", "budget123"]:
            level, _ = estimate_strength(p)
            self.assertEqual(level, "weak", f"{p!r} should be weak")

    def test_repetitive_passphrase_is_weak_even_if_long(self):
        level, _ = estimate_strength("a" * 30)
        self.assertEqual(level, "weak")

    def test_short_passphrase_is_weak(self):
        level, _ = estimate_strength("abc123")
        self.assertEqual(level, "weak")

    def test_long_memorable_phrase_is_strong(self):
        level, _ = estimate_strength("correct horse battery staple")
        self.assertEqual(level, "strong")

    def test_medium_length_with_mixed_classes_is_good_or_strong(self):
        level, _ = estimate_strength("Tr0ub4dor&3xtra")  # 15 chars, 4 character classes
        self.assertIn(level, ("good", "strong"))

    def test_twelve_to_fifteen_chars_single_class_is_fair(self):
        level, _ = estimate_strength("lowercaseonly")
        self.assertEqual(level, "fair")


class TestMeetsMinimum(unittest.TestCase):
    def test_weak_does_not_meet_minimum(self):
        self.assertFalse(meets_minimum("password"))
        self.assertFalse(meets_minimum(""))
        self.assertFalse(meets_minimum("short"))

    def test_fair_or_better_meets_minimum(self):
        self.assertTrue(meets_minimum("a reasonably long passphrase"))
        self.assertTrue(meets_minimum("lowercaseonlybutlong"))


class TestGenerateRecoveryPassphrase(unittest.TestCase):
    def test_default_shape(self):
        key = generate_recovery_passphrase()
        parts = key.split("-")
        self.assertEqual(len(parts), 5)
        self.assertTrue(all(len(p) == 4 for p in parts))

    def test_excludes_ambiguous_characters(self):
        # Generate a bunch and make sure none of the visually ambiguous
        # characters ever show up.
        ambiguous = set("0O1IL")
        for _ in range(50):
            key = generate_recovery_passphrase()
            self.assertFalse(set(key.replace("-", "")) & ambiguous)

    def test_generated_keys_are_unique(self):
        keys = {generate_recovery_passphrase() for _ in range(20)}
        self.assertEqual(len(keys), 20)

    def test_generated_key_meets_strength_minimum(self):
        # The recovery key protects the same data as the primary
        # passphrase -- it should never itself be flagged weak.
        for _ in range(10):
            key = generate_recovery_passphrase()
            self.assertTrue(meets_minimum(key))

    def test_custom_shape(self):
        key = generate_recovery_passphrase(groups=3, group_length=6)
        parts = key.split("-")
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(len(p) == 6 for p in parts))


class TestRecoveryEscrow(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cognibudget_escrow_test_")
        self.db_path = os.path.join(self.tmpdir, "ledger_storage.db")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_escrow_does_not_exist_initially(self):
        self.assertFalse(escrow_exists(self.db_path))

    def test_write_then_read_round_trips(self):
        write_recovery_escrow(self.db_path, "my-primary-passphrase", "RECOVERY-KEY-1")
        self.assertTrue(escrow_exists(self.db_path))
        recovered = read_recovery_escrow(self.db_path, "RECOVERY-KEY-1")
        self.assertEqual(recovered, "my-primary-passphrase")

    def test_wrong_recovery_passphrase_returns_none(self):
        write_recovery_escrow(self.db_path, "my-primary-passphrase", "RECOVERY-KEY-1")
        recovered = read_recovery_escrow(self.db_path, "WRONG-KEY")
        self.assertIsNone(recovered)

    def test_missing_escrow_file_returns_none(self):
        recovered = read_recovery_escrow(self.db_path, "ANYTHING")
        self.assertIsNone(recovered)

    def test_escrow_file_is_permission_restricted(self):
        write_recovery_escrow(self.db_path, "my-primary-passphrase", "RECOVERY-KEY-1")
        path = escrow_path_for(self.db_path)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_delete_removes_escrow(self):
        write_recovery_escrow(self.db_path, "my-primary-passphrase", "RECOVERY-KEY-1")
        delete_recovery_escrow(self.db_path)
        self.assertFalse(escrow_exists(self.db_path))

    def test_delete_when_nothing_exists_does_not_raise(self):
        delete_recovery_escrow(self.db_path)  # should be a no-op, not an error

    def test_corrupt_escrow_file_returns_none_not_raise(self):
        path = escrow_path_for(self.db_path)
        with open(path, "w", encoding="utf-8") as f:
            f.write("not valid json at all {{{")
        recovered = read_recovery_escrow(self.db_path, "RECOVERY-KEY-1")
        self.assertIsNone(recovered)


if __name__ == "__main__":
    unittest.main()
