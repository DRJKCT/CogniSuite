"""
Tests for multi-line (split) transaction support across the cogni-transfer
protocol: payload validation, balance enforcement, backward compatibility
with the legacy 2-line format, and end-to-end posting into the ledger.
"""
import os
import shutil
import tempfile
import unittest
from decimal import Decimal

from protocol import DesktopLedger, parse_amount_strict, PairingError
from desktop_sync_server import (
    _post_transaction_to_ledger,
    _extract_accounts_from_tx,
    SyncContext,
)
from ledger import GeneralLedger


# ---------------------------------------------------------------------------
# A minimal SyncContext stand-in for _post_transaction_to_ledger tests.
# ---------------------------------------------------------------------------
class _FakeSyncContext:
    def __init__(self, gl):
        self.gl = gl


class TestExtractAccountsFromTx(unittest.TestCase):
    def test_new_format_extracts_all_accounts(self):
        tx = {
            "lines": [
                {"account": "Groceries", "type": "DEBIT",  "amount": "60.00"},
                {"account": "Gas",       "type": "DEBIT",  "amount": "40.00"},
                {"account": "Credit Card", "type": "CREDIT", "amount": "100.00"},
            ]
        }
        self.assertEqual(_extract_accounts_from_tx(tx),
                         {"Groceries", "Gas", "Credit Card"})

    def test_legacy_format_extracts_debit_and_credit(self):
        tx = {"debit_account": "Groceries", "credit_account": "Credit Card"}
        self.assertEqual(_extract_accounts_from_tx(tx),
                         {"Groceries", "Credit Card"})

    def test_missing_fields_return_empty_set(self):
        self.assertEqual(_extract_accounts_from_tx({}), set())
        self.assertEqual(_extract_accounts_from_tx({"lines": []}), set())


class TestDesktopLedgerVerifyTransaction(unittest.TestCase):
    def setUp(self):
        self.dl = DesktopLedger()

    # ---- New multi-line format -----------------------------------------
    def test_balanced_3_line_transaction_is_accepted(self):
        tx = {
            "transaction_id": "tx-split-1",
            "date": "2026-08-19",
            "description": "Costco run",
            "lines": [
                {"account": "Groceries",   "type": "DEBIT",  "amount": "60.00"},
                {"account": "Gas",         "type": "DEBIT",  "amount": "40.00"},
                {"account": "Credit Card", "type": "CREDIT", "amount": "100.00"},
            ],
        }
        ok, _ = self.dl.verify_transaction(tx)
        self.assertTrue(ok)

    def test_unbalanced_transaction_is_rejected(self):
        tx = {
            "transaction_id": "tx-split-bad",
            "date": "2026-08-19",
            "description": "off by a penny",
            "lines": [
                {"account": "Groceries",   "type": "DEBIT",  "amount": "60.00"},
                {"account": "Gas",         "type": "DEBIT",  "amount": "40.01"},  # +1¢
                {"account": "Credit Card", "type": "CREDIT", "amount": "100.00"},
            ],
        }
        ok, reason = self.dl.verify_transaction(tx)
        self.assertFalse(ok)
        self.assertIn("out of balance", reason)

    def test_single_line_transaction_is_rejected(self):
        tx = {
            "transaction_id": "tx-one-line",
            "date": "2026-08-19",
            "description": "only one leg",
            "lines": [
                {"account": "Groceries", "type": "DEBIT", "amount": "10.00"},
            ],
        }
        ok, reason = self.dl.verify_transaction(tx)
        self.assertFalse(ok)
        self.assertIn("at least 2 lines", reason)

    def test_single_account_transaction_is_rejected(self):
        # All debits, no credits — technically "balanced" at $0, but
        # nonsensical as a transaction.
        tx = {
            "transaction_id": "tx-one-acct",
            "date": "2026-08-19",
            "description": "all one account",
            "lines": [
                {"account": "Groceries", "type": "DEBIT",  "amount": "10.00"},
                {"account": "Groceries", "type": "CREDIT", "amount": "10.00"},
            ],
        }
        ok, reason = self.dl.verify_transaction(tx)
        self.assertFalse(ok)
        self.assertIn("at least 2 distinct accounts", reason)

    def test_nan_amount_in_any_line_is_rejected(self):
        tx = {
            "transaction_id": "tx-nan",
            "date": "2026-08-19",
            "description": "nan injection",
            "lines": [
                {"account": "Groceries",   "type": "DEBIT",  "amount": "nan"},
                {"account": "Credit Card", "type": "CREDIT", "amount": "10.00"},
            ],
        }
        ok, reason = self.dl.verify_transaction(tx)
        self.assertFalse(ok)
        self.assertIn("Invalid line amount", reason)

    def test_negative_amount_in_any_line_is_rejected(self):
        tx = {
            "transaction_id": "tx-neg",
            "date": "2026-08-19",
            "description": "negative amount",
            "lines": [
                {"account": "Groceries",   "type": "DEBIT",  "amount": "-10.00"},
                {"account": "Credit Card", "type": "CREDIT", "amount": "10.00"},
            ],
        }
        ok, reason = self.dl.verify_transaction(tx)
        self.assertFalse(ok)

    def test_invalid_type_is_rejected(self):
        tx = {
            "transaction_id": "tx-bad-type",
            "date": "2026-08-19",
            "description": "weird type",
            "lines": [
                {"account": "Groceries",   "type": "DEBIT",   "amount": "10.00"},
                {"account": "Credit Card", "type": "REFUND",  "amount": "10.00"},
            ],
        }
        ok, reason = self.dl.verify_transaction(tx)
        self.assertFalse(ok)
        self.assertIn("Invalid line type", reason)

    # ---- Legacy format still works -------------------------------------
    def test_legacy_2_line_format_still_accepted(self):
        tx = {
            "transaction_id": "tx-legacy",
            "date": "2026-08-19",
            "description": "Coffee",
            "amount": "5.00",
            "debit_account": "Groceries",
            "credit_account": "Credit Card",
        }
        ok, _ = self.dl.verify_transaction(tx)
        self.assertTrue(ok)

    def test_legacy_same_account_rejected(self):
        tx = {
            "transaction_id": "tx-legacy-same",
            "date": "2026-08-19",
            "description": "oops",
            "amount": "5.00",
            "debit_account": "Groceries",
            "credit_account": "Groceries",
        }
        ok, reason = self.dl.verify_transaction(tx)
        self.assertFalse(ok)
        self.assertIn("must differ", reason)


class TestPostTransactionToLedgerEndToEnd(unittest.TestCase):
    """
    Full round-trip: build a multi-line transaction payload, hand it to
    _post_transaction_to_ledger, and verify the ledger's balances reflect
    every line correctly.
    """
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cogni_split_test_")
        self.db_path = os.path.join(self.tmpdir, "split.db")
        self.gl = GeneralLedger(self.db_path, "SplitTestPass123!")
        # Ensure all accounts exist before posting.
        for name, acc_type in [
            ("Groceries", "Expense"),
            ("Gas", "Expense"),
            ("Office Supplies", "Expense"),
            ("Credit Card", "Liability"),
            ("Checking Account", "Asset"),
        ]:
            if name not in self.gl.accounts:
                self.gl.create_account(name, acc_type)
        self.ctx = _FakeSyncContext(self.gl)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_3_way_split_posts_and_balances_correctly(self):
        tx = {
            "transaction_id": "costco-001",
            "date": "2026-08-19",
            "description": "Costco run",
            "lines": [
                {"account": "Groceries",       "type": "DEBIT",  "amount": "60.00"},
                {"account": "Gas",             "type": "DEBIT",  "amount": "40.00"},
                {"account": "Credit Card",     "type": "CREDIT", "amount": "100.00"},
            ],
        }
        ok, msg = _post_transaction_to_ledger(self.ctx, tx)
        self.assertTrue(ok, msg)

        self.assertEqual(self.gl.accounts["Groceries"].get_balance(),
                         Decimal("60.00"))
        self.assertEqual(self.gl.accounts["Gas"].get_balance(),
                         Decimal("40.00"))
        self.assertEqual(self.gl.accounts["Credit Card"].get_balance(),
                         Decimal("100.00"))

    def test_4_way_split_with_two_payment_sources(self):
        # $200 office purchase: $120 from credit card, $80 from checking.
        tx = {
            "transaction_id": "office-001",
            "date": "2026-08-19",
            "description": "Office depot",
            "lines": [
                {"account": "Office Supplies", "type": "DEBIT",  "amount": "200.00"},
                {"account": "Credit Card",     "type": "CREDIT", "amount": "120.00"},
                {"account": "Checking Account","type": "CREDIT", "amount": "80.00"},
            ],
        }
        ok, msg = _post_transaction_to_ledger(self.ctx, tx)
        self.assertTrue(ok, msg)

        self.assertEqual(self.gl.accounts["Office Supplies"].get_balance(),
                         Decimal("200.00"))
        self.assertEqual(self.gl.accounts["Credit Card"].get_balance(),
                         Decimal("120.00"))
        # Checking Account is an Asset (normal DR), so a credit REDUCES it.
        self.assertEqual(self.gl.accounts["Checking Account"].get_balance(),
                         Decimal("-80.00"))

    def test_unbalanced_split_is_rejected_by_ledger(self):
        tx = {
            "transaction_id": "bad-001",
            "date": "2026-08-19",
            "description": "off by a penny",
            "lines": [
                {"account": "Groceries",   "type": "DEBIT",  "amount": "60.00"},
                {"account": "Gas",         "type": "DEBIT",  "amount": "40.01"},
                {"account": "Credit Card", "type": "CREDIT", "amount": "100.00"},
            ],
        }
        ok, msg = _post_transaction_to_ledger(self.ctx, tx)
        self.assertFalse(ok)
        self.assertIn("out of balance", msg)
        # And the ledger's balances must be untouched.
        self.assertEqual(self.gl.accounts["Groceries"].get_balance(), Decimal("0.00"))

    def test_unknown_account_in_split_is_rejected(self):
        tx = {
            "transaction_id": "unknown-001",
            "date": "2026-08-19",
            "description": "mystery category",
            "lines": [
                {"account": "Expenses:DoesNotExist", "type": "DEBIT",  "amount": "25.00"},
                {"account": "Credit Card",           "type": "CREDIT", "amount": "25.00"},
            ],
        }
        ok, msg = _post_transaction_to_ledger(self.ctx, tx)
        self.assertFalse(ok)
        self.assertIn("Missing account", msg)

    def test_legacy_flat_format_still_posts(self):
        tx = {
            "transaction_id": "legacy-001",
            "date": "2026-08-19",
            "description": "Coffee",
            "amount": "5.00",
            "debit_account": "Groceries",
            "credit_account": "Credit Card",
        }
        ok, msg = _post_transaction_to_ledger(self.ctx, tx)
        self.assertTrue(ok, msg)
        self.assertEqual(self.gl.accounts["Groceries"].get_balance(),
                         Decimal("5.00"))
        self.assertEqual(self.gl.accounts["Credit Card"].get_balance(),
                         Decimal("5.00"))


if __name__ == "__main__":
    unittest.main()
