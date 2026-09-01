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
        self.gl = GeneralLedger(self.db_path, "test-passphrase-123")

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


class TestTransactionHistory(LedgerTestCase):
    def test_creating_a_transaction_logs_a_created_entry(self):
        self.gl.post_simple_transaction("2026-01-05", "Coffee", "Checking Account", "Groceries", 5.25)
        tx_id = self.gl.transactions[-1].id
        history = self.gl.get_transaction_history(tx_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["change_type"], "created")
        self.assertEqual(history[0]["description"], "Coffee")

    def test_updating_a_transaction_logs_the_prior_state(self):
        self.gl.post_simple_transaction("2026-01-05", "Coffee", "Checking Account", "Groceries", 5.25)
        tx_id = self.gl.transactions[-1].id

        self.gl.update_transaction(
            tx_id, "2026-01-05", "Coffee (corrected)",
            [(None, "Checking Account", 0, 6.75), (None, "Groceries", 6.75, 0)],
        )

        history = self.gl.get_transaction_history(tx_id)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["change_type"], "created")
        self.assertEqual(history[1]["change_type"], "updated")
        # The "updated" entry captures the state *before* the edit
        self.assertEqual(history[1]["description"], "Coffee")
        self.assertEqual(history[1]["lines"][1]["dr"], "5.25")

        # And the ledger now reflects the new state
        current = [t for t in self.gl.transactions if t.id == tx_id][0]
        self.assertEqual(current.description, "Coffee (corrected)")

    def test_deleting_a_transaction_logs_its_final_state_and_survives_deletion(self):
        self.gl.post_simple_transaction("2026-01-05", "Coffee", "Checking Account", "Groceries", 5.25)
        tx_id = self.gl.transactions[-1].id

        ok, msg = self.gl.delete_transaction(tx_id)
        self.assertTrue(ok)

        history = self.gl.get_transaction_history(tx_id)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[1]["change_type"], "deleted")
        self.assertEqual(history[1]["description"], "Coffee")

    def test_get_recent_history_is_most_recent_first(self):
        self.gl.post_simple_transaction("2026-01-05", "Coffee", "Checking Account", "Groceries", 5.25)
        self.gl.post_simple_transaction("2026-01-06", "Lunch", "Checking Account", "Groceries", 12.00)

        recent = self.gl.get_recent_history(limit=10)
        descriptions_in_order = [h["description"] for h in recent]
        self.assertEqual(descriptions_in_order[0], "Lunch")
        self.assertEqual(descriptions_in_order[1], "Coffee")

    def test_history_for_unknown_tx_id_is_empty(self):
        self.assertEqual(self.gl.get_transaction_history(999999), [])

    def test_update_of_nonexistent_transaction_does_not_log_history(self):
        ok, msg = self.gl.update_transaction(
            999999, "2026-01-05", "Ghost", [(None, "Checking Account", 0, 1), (None, "Groceries", 1, 0)]
        )
        self.assertFalse(ok)
        self.assertEqual(self.gl.get_transaction_history(999999), [])


class TestRekey(LedgerTestCase):
    def test_rekey_changes_passphrase_and_db_reopens_with_new_one(self):
        self.gl.post_simple_transaction("2026-01-05", "Payroll", "Salary Income", "Checking Account", 1500.00)
        ok, msg = self.gl.rekey("a-brand-new-passphrase-123")
        self.assertTrue(ok)

        # Old passphrase should no longer work
        with self.assertRaises(ValueError):
            GeneralLedger(self.db_path, "test-passphrase-123")

        # New passphrase should work and data should still be there
        gl2 = GeneralLedger(self.db_path, "a-brand-new-passphrase-123")
        self.assertEqual(len(gl2.transactions), 1)

    def test_rekey_rejects_empty_passphrase(self):
        ok, msg = self.gl.rekey("")
        self.assertFalse(ok)

    def test_rekey_hardens_file_permissions(self):
        import stat
        self.gl.rekey("a-brand-new-passphrase-123")
        mode = stat.S_IMODE(os.stat(self.db_path).st_mode)
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
