"""
test_budget_engine_invariants.py

Verifies two classes of guarantee about the ZBB/envelope budgeting engine:

1. LEDGER ISOLATION -- every budget operation (assign, unassign, move,
   income plan, envelope config) writes only to the sql_budget_* tables.
   The core accounting ledger (sql_transactions, sql_transaction_lines,
   sql_accounts, sql_transaction_history) must be observably identical
   before and after. This is the guarantee that, if ever violated, would
   silently corrupt the accounting equation or fork the audit trail.

2. BUDGET ARITHMETIC -- Ready to Assign, per-envelope net assignments,
   and rollover-policy behavior obey the invariants their own docstrings
   imply. These catch logic regressions inside the budget engine itself,
   independent of whether the ledger was touched.

Note on direction: account renames and deletes DO cascade into the
budget tables by design (update_account re-labels historical
sql_budget_ledger rows, delete_account nulls linked_coa_account). This
test suite exercises the opposite direction only -- budget operations
must not perturb the transaction ledger.

Uses a real (temporary, throwaway) SQLCipher database, matching the
convention of test_balance_invariant_fuzz.py, so behavior is verified
against actual persistence rather than mocks.
"""
import os
import sys
import shutil
import tempfile
import random
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger import GeneralLedger, ZERO
from budget_engine import (
    ready_to_assign,
    envelope_available,
    derive_expected_income,
    net_assignment_in_month,
)


# =========================================================================
# Fingerprint helpers
# =========================================================================

def _ledger_fingerprint(gl: GeneralLedger) -> dict:
    """
    Everything observable about the accounting ledger that a budget
    operation must never change. Shared across every isolation test so
    there's one authoritative definition of "the ledger didn't move."
    """
    tb = gl.trial_balance()
    bs = gl.balance_sheet()
    return {
        "trial_balance_dr": sum((row["dr"] for row in tb), ZERO),
        "trial_balance_cr": sum((row["cr"] for row in tb), ZERO),
        "balance_sheet_totals": {k: bs[k]["total"] for k in bs},
        "transaction_count": len(gl.transactions),
        "tx_ids": [t.id for t in gl.transactions],
        "account_balances": {name: acc.get_balance()
                             for name, acc in gl.accounts.items()},
        "recent_history_count": len(gl.get_recent_history(limit=10_000)),
    }


def _assert_ledger_unchanged(case: unittest.TestCase, before: dict, after: dict, context: str):
    """Field-by-field comparison so a failure names exactly what moved."""
    for key in before:
        case.assertEqual(
            before[key], after[key],
            f"{context}: ledger field '{key}' changed."
        )


# =========================================================================
# Base fixture
# =========================================================================

class _BudgetTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cognibudget_invariants_")
        self.db_path = os.path.join(self.tmpdir, "invariants.db")
        self.gl = GeneralLedger(self.db_path, os.urandom(32).hex())

        # Seed a small, realistic opening state so the ledger isn't empty
        # when budget operations run.
        self.gl.record_transaction(
            "2026-01-01", "Opening deposit",
            [("Checking Account", 1000.00, 0), ("Salary Income", 0, 1000.00)]
        )
        self.gl.record_transaction(
            "2026-01-05", "Groceries",
            [("Groceries", 120.00, 0), ("Checking Account", 0, 120.00)]
        )

        # Two envelopes with different policies, so rollover behavior
        # can be exercised independently of ledger state.
        self.gl.upsert_budget_envelope("Groceries", "Groceries", "reset_monthly", True)
        self.gl.upsert_budget_envelope("Salary Income", None, "carry_forward", True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)


# =========================================================================
# 1. LEDGER ISOLATION
# =========================================================================

class TestBudgetEngineDoesNotTouchLedger(_BudgetTestCase):

    def test_assign_does_not_perturb_ledger(self):
        before = _ledger_fingerprint(self.gl)
        ok, msg = self.gl.assign_to_envelope("2026-01", "Groceries", Decimal("100.00"))
        self.assertTrue(ok, msg)
        _assert_ledger_unchanged(self, before, _ledger_fingerprint(self.gl),
                                 "assign_to_envelope")

    def test_unassign_does_not_perturb_ledger(self):
        self.gl.assign_to_envelope("2026-01", "Groceries", Decimal("100.00"))
        before = _ledger_fingerprint(self.gl)
        ok, msg = self.gl.unassign_from_envelope("2026-01", "Groceries", Decimal("40.00"))
        self.assertTrue(ok, msg)
        _assert_ledger_unchanged(self, before, _ledger_fingerprint(self.gl),
                                 "unassign_from_envelope")

    def test_move_does_not_perturb_ledger(self):
        self.gl.assign_to_envelope("2026-01", "Groceries", Decimal("100.00"))
        before = _ledger_fingerprint(self.gl)
        ok, msg = self.gl.move_budget_money(
            "Groceries", "Salary Income", Decimal("25.00"), "2026-01"
        )
        self.assertTrue(ok, msg)
        _assert_ledger_unchanged(self, before, _ledger_fingerprint(self.gl),
                                 "move_budget_money")

    def test_income_plan_does_not_perturb_ledger(self):
        before = _ledger_fingerprint(self.gl)
        ok, msg = self.gl.set_income_plan(
            "2026-01", Decimal("2000.00"), "manual", None
        )
        self.assertTrue(ok, msg)
        _assert_ledger_unchanged(self, before, _ledger_fingerprint(self.gl),
                                 "set_income_plan")

    def test_envelope_config_does_not_perturb_ledger(self):
        before = _ledger_fingerprint(self.gl)
        ok, msg = self.gl.upsert_budget_envelope(
            "Groceries", None, "carry_forward", True
        )
        self.assertTrue(ok, msg)
        _assert_ledger_unchanged(self, before, _ledger_fingerprint(self.gl),
                                 "upsert_budget_envelope")

    def test_read_only_queries_do_not_perturb_ledger(self):
        """
        The engine's read-side helpers (ready_to_assign, envelope_available,
        derive_expected_income, net_assignment_in_month) are documented as
        pure reads. Verify nothing about the ledger changes when they run.
        """
        self.gl.assign_to_envelope("2026-01", "Groceries", Decimal("100.00"))

        before = _ledger_fingerprint(self.gl)

        ready_to_assign(self.gl, "2026-01")
        envelope_available(self.gl, "Groceries", "2026-01")
        derive_expected_income(self.gl, "2026-01", "last_month")
        derive_expected_income(self.gl, "2026-01", "avg_3mo")
        derive_expected_income(self.gl, "2026-01", "manual")
        net_assignment_in_month(self.gl, "Groceries", "2026-01")

        _assert_ledger_unchanged(self, before, _ledger_fingerprint(self.gl),
                                 "read-only budget queries")

    def test_audit_chain_intact_after_budget_burst(self):
        """
        The cryptographic audit chain must survive an arbitrary sequence
        of budget operations untouched. If any budget write ever slipped
        into sql_transaction_history, this would either fail outright
        (chain break) or show up as a new history row.
        """
        before_history_count = len(self.gl.get_recent_history(limit=10_000))

        for _ in range(5):
            self.gl.assign_to_envelope("2026-01", "Groceries", Decimal("10.00"))
            self.gl.unassign_from_envelope("2026-01", "Groceries", Decimal("5.00"))
            self.gl.move_budget_money("Groceries", "Salary Income", Decimal("1.00"), "2026-01")

        is_valid, msg = self.gl.verify_audit_chain_integrity()
        self.assertTrue(is_valid, f"Audit chain broke after budget burst: {msg}")

        self.assertEqual(
            len(self.gl.get_recent_history(limit=10_000)), before_history_count,
            "Budget operations added rows to the audit history table."
        )


# =========================================================================
# 2. BUDGET ARITHMETIC INVARIANTS
# =========================================================================

class TestBudgetEngineArithmeticInvariants(_BudgetTestCase):

    def test_assign_then_unassign_restores_ready_to_assign(self):
        initial = ready_to_assign(self.gl, "2026-01")
        self.gl.assign_to_envelope("2026-01", "Groceries", Decimal("250.00"))
        self.gl.unassign_from_envelope("2026-01", "Groceries", Decimal("250.00"))
        final = ready_to_assign(self.gl, "2026-01")
        self.assertEqual(initial, final)

    def test_move_does_not_change_ready_to_assign(self):
        self.gl.assign_to_envelope("2026-01", "Groceries", Decimal("100.00"))
        before = ready_to_assign(self.gl, "2026-01")
        self.gl.move_budget_money(
            "Groceries", "Salary Income", Decimal("30.00"), "2026-01"
        )
        after = ready_to_assign(self.gl, "2026-01")
        self.assertEqual(before, after,
                         "Envelope-to-envelope moves must not alter Ready to Assign.")

    def test_envelope_net_assignments_sum_to_total_assigned(self):
        """
        Sum of per-envelope net assignments must equal the month's total
        assigned minus unassigned -- envelope-to-envelope moves cancel out
        across the pool by construction (one move_out for every move_in).
        """
        self.gl.assign_to_envelope("2026-01", "Groceries", Decimal("100.00"))
        self.gl.assign_to_envelope("2026-01", "Salary Income", Decimal("50.00"))
        self.gl.unassign_from_envelope("2026-01", "Groceries", Decimal("20.00"))
        self.gl.move_budget_money("Groceries", "Salary Income", Decimal("30.00"), "2026-01")

        groceries_net = net_assignment_in_month(self.gl, "Groceries", "2026-01")
        salary_net = net_assignment_in_month(self.gl, "Salary Income", "2026-01")
        total_net = groceries_net + salary_net

        # 100 + 50 - 20 = 130 (moves cancel: -30 to Groceries, +30 to Salary)
        self.assertEqual(total_net, Decimal("130.00"))

    def test_reset_monthly_does_not_carry_forward(self):
        self.gl.assign_to_envelope("2026-01", "Groceries", Decimal("500.00"))
        self.gl.assign_to_envelope("2026-02", "Groceries", Decimal("300.00"))

        # reset_monthly: February's available is only February's assignment
        available_feb = envelope_available(self.gl, "Groceries", "2026-02")
        self.assertEqual(available_feb, Decimal("300.00"),
                         "reset_monthly envelope must not carry January's leftover.")

    def test_carry_forward_accumulates_across_months(self):
        self.gl.assign_to_envelope("2026-01", "Salary Income", Decimal("500.00"))
        self.gl.assign_to_envelope("2026-02", "Salary Income", Decimal("300.00"))

        # carry_forward: February's available includes January's full leftover
        available_feb = envelope_available(self.gl, "Salary Income", "2026-02")
        self.assertEqual(available_feb, Decimal("800.00"),
                         "carry_forward envelope must accumulate across months.")

    def test_ready_to_assign_reaches_zero_when_fully_allocated(self):
        income = self.gl.get_total_revenue_for_month("2026-01")
        self.gl.assign_to_envelope("2026-01", "Groceries", income)
        self.assertEqual(ready_to_assign(self.gl, "2026-01"), ZERO)


# =========================================================================
# 3. INTERLEAVED FUZZ
# =========================================================================

FUZZ_ITERATIONS = 40
FUZZ_SEED = 20260911

class TestBudgetAndLedgerInterleaveFuzz(_BudgetTestCase):
    """
    Interleave transaction posts and budget operations, asserting the
    accounting ledger never drifts in response to a budget action.
    Seeded, so any failure reproduces exactly.
    """

    def test_interleaved_operations_preserve_ledger_isolation(self):
        rng = random.Random(FUZZ_SEED)
        actions = ["tx", "assign", "unassign", "move", "income_plan"]

        for i in range(FUZZ_ITERATIONS):
            action = rng.choice(actions)

            if action == "tx":
                amount = Decimal(rng.randint(1, 500)) / 10
                self.gl.record_transaction(
                    "2026-01-15", f"fuzz tx {i}",
                    [("Groceries", amount, 0), ("Checking Account", 0, amount)]
                )
                continue

            before = _ledger_fingerprint(self.gl)
            context = f"iteration={i} action={action} seed={FUZZ_SEED}"

            if action == "assign":
                self.gl.assign_to_envelope("2026-01", "Groceries", Decimal("10.00"))
            elif action == "unassign":
                self.gl.unassign_from_envelope("2026-01", "Groceries", Decimal("5.00"))
            elif action == "move":
                self.gl.move_budget_money(
                    "Groceries", "Salary Income", Decimal("2.00"), "2026-01"
                )
            elif action == "income_plan":
                self.gl.set_income_plan(
                    "2026-01", Decimal("1000.00"), "manual", None
                )

            _assert_ledger_unchanged(self, before, _ledger_fingerprint(self.gl), context)


if __name__ == "__main__":
    unittest.main()
