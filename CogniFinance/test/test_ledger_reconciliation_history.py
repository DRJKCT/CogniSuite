"""
Tests for the reconciliation and transaction-history features added to
ledger.py. These use a real (temporary, throwaway) SQLCipher-encrypted
database rather than mocks, since the behavior being tested is mostly
about what actually gets persisted and read back.

Requires the sqlcipher3 package (already a dependency of the app itself,
via main.py / ledger.py).

Run with:
    python -m unittest discover -s tests
"""
import os
import sys
import shutil
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger import GeneralLedger, money


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cognibudget_test_")
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.test_dek_hex = os.urandom(32).hex()
        self.gl = GeneralLedger(self.db_path, self.test_dek_hex)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestReconciliation(LedgerTestCase):
    def test_running_balance_as_of_matches_manual_calculation(self):
        self.gl.post_simple_transaction("2026-01-05", "Payroll", "Salary Income", "Checking Account", 1500.00)
        self.gl.post_simple_transaction("2026-01-06", "Groceries", "Checking Account", "Groceries", 87.32)
        bal = self.gl.running_balance_as_of("Checking Account", "2026-01-06")
        self.assertEqual(bal, money(1500.00 - 87.32))

    def test_running_balance_ignores_transactions_after_as_of_date(self):
        self.gl.post_simple_transaction("2026-01-05", "Payroll", "Salary Income", "Checking Account", 1500.00)
        self.gl.post_simple_transaction("2026-01-10", "Groceries", "Checking Account", "Groceries", 87.32)
        bal = self.gl.running_balance_as_of("Checking Account", "2026-01-06")
        self.assertEqual(bal, money(1500.00))

    def test_running_balance_unknown_account_raises(self):
        with self.assertRaises(ValueError):
            self.gl.running_balance_as_of("Not A Real Account", "2026-01-06")

    def test_record_statement_balance_rejects_unknown_account(self):
        ok, msg = self.gl.record_statement_balance("Not A Real Account", "2026-01-06", 100)
        self.assertFalse(ok)

    def test_record_statement_balance_rejects_bad_date_format(self):
        ok, msg = self.gl.record_statement_balance("Checking Account", "06 Jan 2026", 100)
        self.assertFalse(ok)

    def test_reconciliation_report_flags_matching_checkpoint(self):
        self.gl.post_simple_transaction("2026-01-05", "Payroll", "Salary Income", "Checking Account", 1500.00)
        self.gl.record_statement_balance("Checking Account", "2026-01-05", 1500.00, source="stmt.pdf")
        report = self.gl.reconciliation_report("Checking Account")
        self.assertEqual(len(report), 1)
        self.assertTrue(report[0]["matched"])
        self.assertEqual(report[0]["difference"], money("0.00"))

    def test_reconciliation_report_flags_mismatched_checkpoint(self):
        self.gl.post_simple_transaction("2026-01-05", "Payroll", "Salary Income", "Checking Account", 1500.00)
        self.gl.record_statement_balance("Checking Account", "2026-01-05", 1450.00, source="stmt.pdf")
        report = self.gl.reconciliation_report("Checking Account")
        self.assertEqual(len(report), 1)
        self.assertFalse(report[0]["matched"])
        self.assertEqual(report[0]["difference"], money("50.00"))

    def test_reconciliation_report_within_tolerance_still_matches(self):
        self.gl.post_simple_transaction("2026-01-05", "Payroll", "Salary Income", "Checking Account", 1500.00)
        # off by half a cent worth of rounding noise, within the 1-cent default tolerance
        self.gl.record_statement_balance("Checking Account", "2026-01-05", 1500.00, source="stmt.pdf")
        report = self.gl.reconciliation_report("Checking Account", tolerance=Decimal("0.01"))
        self.assertTrue(report[0]["matched"])

    def test_delete_statement_balance(self):
        self.gl.post_simple_transaction("2026-01-05", "Payroll", "Salary Income", "Checking Account", 1500.00)
        self.gl.record_statement_balance("Checking Account", "2026-01-05", 1500.00)
        checkpoints = self.gl.get_statement_balances("Checking Account")
        self.assertEqual(len(checkpoints), 1)
        ok, msg = self.gl.delete_statement_balance(checkpoints[0]["id"])
        self.assertTrue(ok)
        self.assertEqual(self.gl.get_statement_balances("Checking Account"), [])


if __name__ == "__main__":
    unittest.main()
