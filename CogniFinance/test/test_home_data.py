"""
test_home_data.py

Home must show the ledger's numbers, not its own. These tests build a
small fake ledger and check each card against the call that feeds it.
"""
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import budget_engine
import home_data
from test_budget_summary import FakeLedger

D = Decimal


@dataclass
class Line:
    account_name: str
    dr: Decimal = D("0.00")
    cr: Decimal = D("0.00")


@dataclass
class Tx:
    id: int
    date: str
    description: str
    lines: List[Line] = field(default_factory=list)
    is_voided: bool = False
    reverses_tx_id: object = None


class Acct:
    def __init__(self, account_type, balance="0.00", report_line=None):
        self.account_type = account_type
        self.report_line = report_line
        self._balance = D(balance)

    def get_balance(self):
        return self._balance


class HomeFake(FakeLedger):
    def __init__(self):
        super().__init__()
        self.transactions = []

    def add_account(self, name, account_type, balance="0.00", report_line=None):
        self.accounts[name] = Acct(account_type, balance, report_line)

    def add_tx(self, tx):
        self.transactions.append(tx)

    def get_transactions(self, start_date=None, end_date=None, **_):
        return [t for t in self.transactions
                if (not start_date or t.date >= start_date)
                and (not end_date or t.date <= end_date)]

    def monthly_income_expense_report(self, year, month):
        prefix = f"{year:04d}-{month:02d}"
        expense_total = D("0.00")
        by_account = {}
        for tx in self.transactions:
            if not tx.date.startswith(prefix):
                continue
            for line in tx.lines:
                if self.accounts[line.account_name].account_type == "Expense":
                    net = line.dr - line.cr
                    expense_total += net
                    by_account[line.account_name] = by_account.get(line.account_name, D("0.00")) + net
        by_account = {k: v for k, v in by_account.items() if v != 0}
        return {"expense_total": expense_total, "expense_by_account": by_account}


def amount_fn(ledger, tx):
    """Stand-in for ui_helpers.get_transaction_display_amount."""
    for line in tx.lines:
        acc = ledger.accounts[line.account_name]
        if acc.account_type == "Expense":
            return -line.dr
        if acc.account_type == "Revenue":
            return line.cr
    return D("0.00")


NOW = datetime(2026, 10, 24, 9, 30)


def make():
    gl = HomeFake()
    gl.add_account("Checking", "Asset", "1200.50", "Cash and Cash Equivalents")
    gl.add_account("Savings", "Asset", "10500.00", "Cash and Cash Equivalents")
    gl.add_account("Visa", "Liability", "450.80", "Credit Cards")
    gl.add_account("Loose", "Asset", "20.00", None)               # no report line: included
    gl.add_account("House", "Asset", "300000.00", "Property, Plant & Equipment")  # excluded
    gl.add_account("Groceries Expense", "Expense")
    gl.add_account("Dining Expense", "Expense")
    gl.add_account("Salary", "Revenue")

    gl.add_envelope("Groceries", "Groceries Expense", "carry_forward")
    gl.add_envelope("Dining", "Dining Expense", "reset_monthly")
    gl.set_revenue("2026-10", "3200")
    gl.add_row("2026-10", "Groceries", "assign", "300")
    gl.add_row("2026-10", "Dining", "assign", "100")

    gl.add_tx(Tx(1, "2026-10-02", "Weekly shop",
                 [Line("Groceries Expense", dr=D("120.00")), Line("Checking", cr=D("120.00"))]))
    gl.add_tx(Tx(2, "2026-10-09", "Pizza",
                 [Line("Dining Expense", dr=D("130.00")), Line("Visa", cr=D("130.00"))]))
    gl.add_tx(Tx(3, "2026-10-15", "Paycheque",
                 [Line("Checking", dr=D("3200.00")), Line("Salary", cr=D("3200.00"))]))
    gl.add_tx(Tx(4, "2026-10-20", "Shop again",
                 [Line("Groceries Expense", dr=D("80.00")), Line("Checking", cr=D("80.00"))]))
    gl.set_spent("Groceries Expense", "2026-10", "200")
    gl.set_spent("Dining Expense", "2026-10", "130")
    return gl


def build(gl, **kw):
    return home_data.build_home_data(gl, NOW, amount_fn, **kw)


def test_greeting_by_hour_and_name():
    assert home_data.greeting_for(datetime(2026, 1, 1, 8), "Sam") == "Good morning, Sam!"
    assert home_data.greeting_for(datetime(2026, 1, 1, 13), None) == "Good afternoon!"
    assert home_data.greeting_for(datetime(2026, 1, 1, 19), "  ") == "Good evening!"


def test_date_line():
    assert home_data.date_line_for(NOW.date()) == "Saturday, October 24, 2026"


def test_spending_total_is_the_ledger_total():
    gl = make()
    data = build(gl)
    assert data.spending.total == gl.monthly_income_expense_report(2026, 10)["expense_total"]
    assert data.spending.total == D("330.00")


def test_sparkline_ends_at_the_headline_total():
    data = build(make())
    assert data.spending.cumulative[-1] == data.spending.total
    assert data.spending.cumulative == sorted(data.spending.cumulative)   # never goes down


def test_a_refund_lowers_the_total_and_the_sparkline_still_ties():
    gl = make()
    gl.add_tx(Tx(30, "2026-10-21", "Refund",
                 [Line("Checking", dr=D("20.00")), Line("Groceries Expense", cr=D("20.00"))]))
    data = build(gl)
    assert data.spending.total == D("310.00")
    assert data.spending.cumulative[-1] == data.spending.total
    assert dict(data.spending.top_categories)["Groceries Expense"] == D("180.00")


def test_a_voided_expense_nets_out_of_the_total_and_the_sparkline():
    gl = make()
    gl.transactions[1].is_voided = True                      # Pizza, 130
    gl.add_tx(Tx(31, "2026-10-09", "VOID: Pizza",
                 [Line("Visa", dr=D("130.00")), Line("Dining Expense", cr=D("130.00"))],
                 reverses_tx_id=2))
    data = build(gl)
    assert data.spending.total == D("200.00")
    assert data.spending.cumulative[-1] == D("200.00")
    assert "Dining Expense" not in dict(data.spending.top_categories)


def test_future_dated_charge_still_ties_to_total():
    gl = make()
    gl.add_tx(Tx(5, "2026-10-28", "Scheduled",
                 [Line("Dining Expense", dr=D("50.00")), Line("Visa", cr=D("50.00"))]))
    data = build(gl)
    assert data.spending.total == D("380.00")
    assert data.spending.cumulative[-1] == data.spending.total


def test_top_categories_ranked_and_limited():
    data = build(make(), top_categories=2)
    assert data.spending.top_categories == [("Groceries Expense", D("200.00")),
                                            ("Dining Expense", D("130.00"))]


def test_accounts_are_everyday_only_and_flag_what_is_owed():
    data = build(make(), account_limit=10)
    names = [a.name for a in data.accounts]
    assert "House" not in names and "Groceries Expense" not in names
    assert names == ["Savings", "Checking", "Loose", "Visa"]      # money first, then owed
    assert [a.owed for a in data.accounts] == [False, False, False, True]
    assert data.accounts[-1].balance == D("450.80")               # ledger value, unchanged


def test_accounts_state_their_scope():
    data = build(make(), account_limit=2)
    assert len(data.accounts) == 2 and data.accounts_total == 4


def test_recent_is_newest_first_limited_and_amounts_come_from_amount_fn():
    gl = make()
    data = build(gl, recent_limit=3)
    assert [r.description for r in data.recent] == ["Shop again", "Paycheque", "Pizza"]
    assert data.recent[0].amount == amount_fn(gl, gl.transactions[-1]) == D("-80.00")
    assert data.recent[0].category == "Groceries Expense"
    assert data.recent[1].category == "Salary"


def test_recent_leaves_out_voided_pairs():
    gl = make()
    gl.transactions[1].is_voided = True
    gl.add_tx(Tx(6, "2026-10-21", "Reversal of Pizza",
                 [Line("Dining Expense", cr=D("130.00")), Line("Visa", dr=D("130.00"))],
                 reverses_tx_id=2))
    descriptions = [r.description for r in build(gl, recent_limit=10).recent]
    assert "Pizza" not in descriptions and "Reversal of Pizza" not in descriptions


def test_split_and_transfer_labels():
    gl = make()
    gl.add_tx(Tx(7, "2026-10-22", "Costco",
                 [Line("Groceries Expense", dr=D("40.00")), Line("Dining Expense", dr=D("10.00")),
                  Line("Checking", cr=D("50.00"))]))
    gl.add_tx(Tx(8, "2026-10-23", "Move money",
                 [Line("Savings", dr=D("100.00")), Line("Checking", cr=D("100.00"))]))
    recent = build(gl, recent_limit=2).recent
    assert recent[0].category == "Transfer"
    assert recent[1].category == "Split (2)"


def test_budget_card_matches_engine_and_states_scope():
    gl = make()
    data = build(gl, envelope_limit=1)
    assert data.budget.left_to_assign == budget_engine.ready_to_assign(gl, "2026-10")
    assert data.budget.total_envelopes == 2 and len(data.budget.shown) == 1
    for row in build(gl).budget.shown:
        assert row.available == budget_engine.envelope_available(gl, row.category, "2026-10")


def test_overspent_envelopes_come_first_and_are_counted():
    gl = make()                      # Dining: 100 assigned, 130 spent
    data = build(gl)
    assert data.budget.shown[0].category == "Dining"
    assert data.budget.shown[0].state == "overspent"
    assert data.budget.shown[0].fraction == 1.0
    assert data.budget.overspent_count == 1


def test_bar_fraction_uses_carried_money():
    gl = HomeFake()
    gl.add_envelope("Rent", "Rent Expense", "carry_forward")
    gl.accounts["Rent Expense"] = Acct("Expense")
    gl.add_row("2026-09", "Rent", "assign", "100")
    gl.set_spent("Rent Expense", "2026-09", "30")        # 70 carried in
    gl.add_row("2026-10", "Rent", "assign", "100")
    gl.set_spent("Rent Expense", "2026-10", "85")
    row = build(gl).budget.shown[0]
    assert row.funds == D("170.00") and row.available == D("85.00")
    assert abs(row.fraction - 0.5) < 1e-9
