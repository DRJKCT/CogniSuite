"""
test_budget_summary.py

The shared envelope summary must be the budget engine's numbers and
nothing else. These tests build a small fake ledger that implements
only the methods budget_engine calls, then check the summary against
the engine functions directly.

A fake is used so the tests are fast and need no database. Add one
integration test against a real GeneralLedger fixture once you have a
convenient way to build one; the equivalence assertions below are the
ones to repeat there.
"""
import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import budget_engine
import budget_summary
from budget_summary import STATE_EMPTY, STATE_OK, STATE_OVERSPENT


D = Decimal


class FakeAccount:
    def __init__(self, account_type="Expense"):
        self.account_type = account_type


class FakeLedger:
    """Just enough of GeneralLedger for budget_engine + budget_summary."""

    def __init__(self):
        self.accounts = {}
        self._envelopes = []       # dicts as get_budget_envelopes returns
        self._rows = []            # ledger rows
        self._actuals = {}         # (account, month) -> Decimal
        self._revenue = {}         # month -> Decimal

    # --- setup helpers -------------------------------------------------
    def add_envelope(self, name, linked, policy="carry_forward", active=True):
        if linked:
            self.accounts.setdefault(linked, FakeAccount())
        self._envelopes.append({
            "category_name": name, "linked_coa_account": linked,
            "rollover_policy": policy, "is_active": active,
        })

    def add_row(self, month, category, event_type, amount):
        self._rows.append({"month": month, "category_name": category,
                           "event_type": event_type, "amount": D(amount)})
        self._rows.sort(key=lambda r: r["month"])

    def set_spent(self, account, month, amount):
        self._actuals[(account, month)] = D(amount)

    def set_revenue(self, month, amount):
        self._revenue[month] = D(amount)

    # --- the ledger surface the engine uses ----------------------------
    def get_budget_envelopes(self, include_inactive=False):
        return [dict(e) for e in self._envelopes
                if include_inactive or e["is_active"]]

    def get_budget_ledger_rows(self, month):
        return [r for r in self._rows if r["month"] == month]

    def get_budget_ledger_rows_for_category(self, category):
        return [r for r in self._rows if r["category_name"] == category]

    def get_category_actuals(self, linked, month):
        return self._actuals.get((linked, month), D("0.00"))

    def get_total_revenue_for_month(self, month):
        return self._revenue.get(month, D("0.00"))


@pytest.fixture
def ledger():
    gl = FakeLedger()
    gl.add_envelope("Groceries", "Groceries Expense", "carry_forward")
    gl.add_envelope("Dining", "Dining Expense", "reset_monthly")
    gl.add_envelope("Fun", None, "carry_forward")                 # no link
    gl.add_envelope("Old", "Old Expense", "carry_forward", active=False)

    gl.set_revenue("2026-09", "3000")
    gl.set_revenue("2026-10", "3200")

    # September
    gl.add_row("2026-09", "Groceries", "assign", "400")
    gl.add_row("2026-09", "Dining", "assign", "150")
    gl.set_spent("Groceries Expense", "2026-09", "430")   # 30 over
    gl.set_spent("Dining Expense", "2026-09", "200")      # 50 over

    # October, including a move between envelopes
    gl.add_row("2026-10", "Groceries", "assign", "300")
    gl.add_row("2026-10", "Dining", "assign", "100")
    gl.add_row("2026-10", "Dining", "move_out", "20")
    gl.add_row("2026-10", "Groceries", "move_in", "20")
    gl.set_spent("Groceries Expense", "2026-10", "250")
    gl.set_spent("Dining Expense", "2026-10", "60")
    return gl


def _by_name(summary):
    return {e.category: e for e in summary.envelopes}


def test_available_equals_engine_for_every_envelope(ledger):
    s = budget_summary.summarize(ledger, "2026-10")
    for env in s.envelopes:
        assert env.available == budget_engine.envelope_available(
            ledger, env.category, "2026-10"), env.category


def test_assigned_and_spent_equal_engine(ledger):
    s = budget_summary.summarize(ledger, "2026-10")
    for env in s.envelopes:
        assert env.assigned == budget_engine.net_assignment_in_month(
            ledger, env.category, "2026-10"), env.category
        if env.tracks_spending:
            assert env.spent == ledger.get_category_actuals(
                env.linked_account, "2026-10"), env.category


def test_left_to_assign_equals_engine(ledger):
    s = budget_summary.summarize(ledger, "2026-10")
    assert s.left_to_assign == budget_engine.ready_to_assign(ledger, "2026-10")


def test_carry_forward_keeps_earlier_overspend(ledger):
    groceries = _by_name(budget_summary.summarize(ledger, "2026-10"))["Groceries"]
    # Sep: 400 - 430 = -30.  Oct: -30 + (300 + 20) - 250 = 40.
    assert groceries.available == D("40.00")
    assert groceries.carried_in == D("-30.00")
    assert groceries.state == STATE_OK


def test_overspend_stays_negative_and_is_not_clamped():
    gl = FakeLedger()
    gl.add_envelope("Rent", "Rent Expense", "carry_forward")
    gl.add_row("2026-10", "Rent", "assign", "100")
    gl.set_spent("Rent Expense", "2026-10", "140")
    rent = _by_name(budget_summary.summarize(gl, "2026-10"))["Rent"]
    assert rent.available == D("-40.00")
    assert rent.state == STATE_OVERSPENT


def test_reset_monthly_ignores_earlier_months(ledger):
    dining = _by_name(budget_summary.summarize(ledger, "2026-10"))["Dining"]
    # Oct only: (100 - 20) - 60 = 20.  September's overspend does not carry.
    assert dining.available == D("20.00")
    assert dining.carried_in == D("0.00")


def test_envelope_without_link_is_flagged_not_silent(ledger):
    fun = _by_name(budget_summary.summarize(ledger, "2026-10"))["Fun"]
    assert fun.tracks_spending is False
    assert fun.spent == D("0.00")


def test_inactive_envelopes_are_excluded(ledger):
    names = [e.category for e in budget_summary.summarize(ledger, "2026-10").envelopes]
    assert "Old" not in names


def test_empty_state():
    gl = FakeLedger()
    gl.add_envelope("Gifts", "Gifts Expense", "carry_forward")
    gifts = _by_name(budget_summary.summarize(gl, "2026-10"))["Gifts"]
    assert gifts.state == STATE_EMPTY


def test_order_rule_overspent_first_most_overspent_first():
    gl = FakeLedger()
    for name, assigned, spent in [("A", "100", "50"),    # +50
                                  ("B", "100", "130"),   # -30
                                  ("C", "100", "180"),   # -80
                                  ("D", "100", "90")]:   # +10
        gl.add_envelope(name, f"{name} Expense", "carry_forward")
        gl.add_row("2026-10", name, "assign", assigned)
        gl.set_spent(f"{name} Expense", "2026-10", spent)
    order = [e.category for e in budget_summary.summarize(gl, "2026-10").envelopes]
    assert order == ["C", "B", "D", "A"]


def test_overspent_property_lists_only_overspent(ledger):
    gl = FakeLedger()
    gl.add_envelope("X", "X Expense", "carry_forward")
    gl.add_row("2026-10", "X", "assign", "10")
    gl.set_spent("X Expense", "2026-10", "20")
    assert [e.category for e in budget_summary.summarize(gl, "2026-10").overspent] == ["X"]


def test_policy_sentences_are_distinct_and_unknown_is_flagged():
    assert budget_summary.policy_sentence("carry_forward") != \
        budget_summary.policy_sentence("reset_monthly")
    assert "not recognized" in budget_summary.policy_sentence("bogus").lower()
