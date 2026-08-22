# test_protocol_equivalence.py
import unittest
from decimal import Decimal
from protocol import DesktopLedger, parse_amount_strict

class TestTransactionEquivalence(unittest.TestCase):
    def setUp(self):
        self.ledger = DesktopLedger()

    def _make_legacy(self, tx_id="tx-1", date="2024-01-01", desc="Test",
                     dr_acc="Expenses", cr_acc="Bank", amount="100.00"):
        return {
            "transaction_id": tx_id, "date": date, "description": desc,
            "debit_account": dr_acc, "credit_account": cr_acc, "amount": amount
        }

    def _make_new(self, tx_id="tx-1", date="2024-01-01", desc="Test", lines=None):
        return {
            "transaction_id": tx_id, "date": date, "description": desc,
            "lines": lines or []
        }

    # ==========================================
    # 1. LEGACY FORMAT TESTS
    # ==========================================
    def test_legacy_identical_is_idempotent(self):
        """Exact same legacy transaction should be treated as a successful retry."""
        a = self._make_legacy()
        b = self._make_legacy()
        self.assertTrue(self.ledger._transactions_equivalent(a, b))

    def test_legacy_different_amount_is_conflict(self):
        """Same ID, different amount = Conflict (User edited it)."""
        a = self._make_legacy(amount="100.00")
        b = self._make_legacy(amount="150.00")
        self.assertFalse(self.ledger._transactions_equivalent(a, b))

    def test_legacy_different_account_is_conflict(self):
        a = self._make_legacy(dr_acc="Expenses:Groceries")
        b = self._make_legacy(dr_acc="Expenses:Gas")
        self.assertFalse(self.ledger._transactions_equivalent(a, b))

    # ==========================================
    # 2. NEW MULTI-LINE FORMAT TESTS
    # ==========================================
    def test_new_identical_is_idempotent(self):
        """Exact same split transaction should be treated as a successful retry."""
        lines = [
            {"account": "Groceries", "type": "DEBIT", "amount": "60.00"},
            {"account": "Gas", "type": "DEBIT", "amount": "40.00"},
            {"account": "Credit Card", "type": "CREDIT", "amount": "100.00"}
        ]
        a = self._make_new(lines=lines)
        b = self._make_new(lines=lines)
        self.assertTrue(self.ledger._transactions_equivalent(a, b))

    def test_new_different_amount_is_conflict(self):
        """User changed a split amount on the phone."""
        lines_a = [
            {"account": "Groceries", "type": "DEBIT", "amount": "60.00"},
            {"account": "Credit Card", "type": "CREDIT", "amount": "60.00"}
        ]
        lines_b = [
            {"account": "Groceries", "type": "DEBIT", "amount": "50.00"}, # Changed
            {"account": "Credit Card", "type": "CREDIT", "amount": "50.00"}
        ]
        self.assertFalse(self.ledger._transactions_equivalent(self._make_new(lines=lines_a), self._make_new(lines=lines_b)))

    def test_new_decimal_equivalence(self):
        """'100.0' and '100.00' must be treated as identical (no false conflicts)."""
        lines_a = [{"account": "A", "type": "DEBIT", "amount": "100.0"}, {"account": "B", "type": "CREDIT", "amount": "100.0"}]
        lines_b = [{"account": "A", "type": "DEBIT", "amount": "100.00"}, {"account": "B", "type": "CREDIT", "amount": "100.00"}]
        self.assertTrue(self.ledger._transactions_equivalent(self._make_new(lines=lines_a), self._make_new(lines=lines_b)))

    # ==========================================
    # 3. THE CRITICAL EDGE CASE: ORDER INDEPENDENCE
    # ==========================================
    def test_new_order_independence(self):
        """
        CRITICAL: If the phone sends splits in a different order, it MUST
        still be recognized as the exact same transaction, not a conflict.
        """
        lines_a = [
            {"account": "Groceries", "type": "DEBIT", "amount": "60.00"},
            {"account": "Gas", "type": "DEBIT", "amount": "40.00"},
            {"account": "Credit Card", "type": "CREDIT", "amount": "100.00"}
        ]
        lines_b = [
            {"account": "Gas", "type": "DEBIT", "amount": "40.00"},       # Swapped order
            {"account": "Credit Card", "type": "CREDIT", "amount": "100.00"},
            {"account": "Groceries", "type": "DEBIT", "amount": "60.00"}  # Swapped order
        ]
        self.assertTrue(self.ledger._transactions_equivalent(self._make_new(lines=lines_a), self._make_new(lines=lines_b)))

    # ==========================================
    # 4. MIXED / MALFORMED TESTS
    # ==========================================
    def test_mixed_formats_is_false(self):
        """One legacy, one new format = fundamentally different shapes."""
        a = self._make_legacy()
        b = self._make_new(lines=[{"account": "A", "type": "DEBIT", "amount": "100.00"}, {"account": "B", "type": "CREDIT", "amount": "100.00"}])
        self.assertFalse(self.ledger._transactions_equivalent(a, b))

    def test_different_line_counts_is_conflict(self):
        """User added or removed a split on the phone."""
        lines_a = [{"account": "A", "type": "DEBIT", "amount": "100.00"}, {"account": "B", "type": "CREDIT", "amount": "100.00"}]
        lines_b = [
            {"account": "A", "type": "DEBIT", "amount": "50.00"},
            {"account": "C", "type": "DEBIT", "amount": "50.00"},
            {"account": "B", "type": "CREDIT", "amount": "100.00"}
        ]
        self.assertFalse(self.ledger._transactions_equivalent(self._make_new(lines=lines_a), self._make_new(lines=lines_b)))

    def test_invalid_amount_falls_back_safely(self):
        """If an amount is completely malformed, it shouldn't crash, just return False."""
        lines_a = [{"account": "A", "type": "DEBIT", "amount": "100.00"}, {"account": "B", "type": "CREDIT", "amount": "100.00"}]
        lines_b = [{"account": "A", "type": "DEBIT", "amount": "NaN"}, {"account": "B", "type": "CREDIT", "amount": "100.00"}]
        self.assertFalse(self.ledger._transactions_equivalent(self._make_new(lines=lines_a), self._make_new(lines=lines_b)))

if __name__ == '__main__':
    unittest.main()
