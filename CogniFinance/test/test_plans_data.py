"""test_plans_data.py: the small decisions behind the Plans screen."""
import os
import sys
from datetime import datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import budget_summary
import plans_data
from test_budget_summary import FakeLedger, FakeAccount

D = Decimal


def test_month_helpers():
    assert plans_data.month_of(datetime(2026, 10, 25)) == "2026-10"
    assert plans_data.shift_month("2026-10", 1) == "2026-11"
    assert plans_data.shift_month("2026-12", 1) == "2027-01"
    assert plans_data.shift_month("2026-01", -1) == "2025-12"
    assert plans_data.shift_month("2026-03", -15) == "2024-12"
    assert plans_data.month_title("2026-10") == "October 2026"


def ledger():
    gl = FakeLedger()
    for name in ("Groceries Expense", "Dining Expense", "Spare Expense"):
        gl.accounts[name] = FakeAccount("Expense")
    gl.accounts["Checking"] = FakeAccount("Asset")
    gl.add_envelope("Groceries", "Groceries Expense")
    gl.add_envelope("Old", "Spare Expense", active=False)       # archived but still owns its account
    return gl


def test_free_accounts_exclude_ones_any_envelope_owns_even_archived():
    gl = ledger()
    assert plans_data.free_expense_accounts(gl) == ["Dining Expense"]
    assert plans_data.free_expense_accounts(gl, keep="Groceries Expense") == ["Dining Expense", "Groceries Expense"]
    assert plans_data.free_expense_accounts(gl, exclude_category="Groceries") == ["Dining Expense", "Groceries Expense"]


def test_validate_new_envelope():
    gl = ledger()
    v = plans_data.validate_new_envelope
    assert v(gl, "Dining", "Dining Expense") is None
    assert "name" in v(gl, " ", "Dining Expense").lower()
    assert "already exists" in v(gl, "groceries", "Dining Expense")
    assert "archived" in v(gl, "Old", "Dining Expense")
    assert "tracks" in v(gl, "Fun", None)
    assert "not an expense" in v(gl, "Fun", "Checking")
    assert "already tracked" in v(gl, "Fun", "Groceries Expense")


def make_summary():
    gl = FakeLedger()
    for name in ("A", "B", "C"):
        gl.add_envelope(name, f"{name} Expense")
        gl.add_row("2026-10", name, "assign", "100")
    gl.set_spent("A Expense", "2026-10", "130")     # over by 30
    gl.set_spent("B Expense", "2026-10", "40")      # 60 left
    gl.set_spent("C Expense", "2026-10", "10")      # 90 left
    return budget_summary.summarize(gl, "2026-10")


def test_shortfall_and_cover_sources():
    s = make_summary()
    a = next(e for e in s.envelopes if e.category == "A")
    assert plans_data.shortfall(a) == D("30.00")
    assert plans_data.shortfall(next(e for e in s.envelopes if e.category == "B")) == D("0.00")
    assert [e.category for e in plans_data.cover_sources(s, "A")] == ["C", "B"]
    assert [e.category for e in plans_data.cover_sources(s, "C")] == ["B"]      # A is overspent, B has 60


def test_carried_sentence():
    s = make_summary()
    a = s.envelopes[0]
    fmt = lambda v: f"${v}"
    assert plans_data.carried_sentence(a, fmt) == ""
    import dataclasses
    assert "carried over" in plans_data.carried_sentence(dataclasses.replace(a, carried_in=D("70.00")), fmt)
    assert "short" in plans_data.carried_sentence(dataclasses.replace(a, carried_in=D("-30.00")), fmt)
