"""
test_expense_netting.py

The ledger is append-only: voiding a transaction posts a reversing entry,
editing posts a reversal plus a new entry, and a refund credits the
expense account. Spending figures must therefore net credits against
debits, as the revenue figures already do, or a voided expense keeps
counting and an edited one counts twice.

These tests call the real GeneralLedger methods on an in-memory ledger
(no database), and budget_engine on top of them.
"""
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import budget_engine
from ledger import GeneralLedger, Account, Transaction, TransactionLine as L, ZERO

D = Decimal
OCT = "2026-10"


class MemoryLedger(GeneralLedger):
    """GeneralLedger with in-memory budget rows instead of a database."""

    def get_budget_envelopes(self, include_inactive=False):
        return [{"category_name": "Groceries", "linked_coa_account": "Groceries Expense",
                 "rollover_policy": "carry_forward", "is_active": True}]

    def get_budget_ledger_rows_for_category(self, category):
        return [r for r in self._budget_rows if r["category_name"] == category]

    def get_budget_ledger_rows(self, month):
        return [r for r in self._budget_rows if r["month"] == month]


def make():
    gl = object.__new__(MemoryLedger)
    gl.accounts = {
        "Checking": Account("Checking", "Asset"),
        "Groceries Expense": Account("Groceries Expense", "Expense"),
        "Dining Expense": Account("Dining Expense", "Expense"),
        "Medical Expense": Account("Medical Expense", "Expense"),
        "Salary": Account("Salary", "Revenue"),
    }
    gl.transactions = []
    gl._budget_rows = [{"month": OCT, "category_name": "Groceries",
                        "event_type": "assign", "amount": D("300.00")}]
    return gl


_next = [1]


def post(gl, date, description, lines, **kw):
    tx = Transaction(_next[0], date, description,
                     [L(acc, D(dr), D(cr)) for acc, dr, cr in lines], **kw)
    _next[0] += 1
    gl.transactions.append(tx)
    return tx


def void(gl, tx):
    """What GeneralLedger.void_transaction leaves in the books."""
    tx.is_voided = True
    return post(gl, tx.date, f"VOID: {tx.description}",
                [(l.account_name, l.cr, l.dr) for l in tx.lines], reverses_tx_id=tx.id)


def spend(gl, amount, account="Groceries Expense", date="2026-10-05", desc="Shop"):
    return post(gl, date, desc, [(account, amount, "0"), ("Checking", "0", amount)])


def figures(gl, account="Groceries Expense", month=OCT):
    y, m = map(int, month.split("-"))
    report = gl.monthly_income_expense_report(y, m)
    return (gl.get_category_actuals(account, month), report["expense_total"],
            gl.spending_by_category(month).get(account, ZERO))


def test_a_plain_expense_counts_once_everywhere():
    gl = make()
    spend(gl, "120.00")
    assert figures(gl) == (D("120.00"), D("120.00"), D("120.00"))


def test_a_voided_expense_nets_to_zero_everywhere():
    gl = make()
    void(gl, spend(gl, "120.00"))
    assert figures(gl) == (ZERO, ZERO, ZERO)
    report = gl.monthly_income_expense_report(2026, 10)
    assert "Groceries Expense" not in report["expense_by_account"]
    assert "Groceries Expense" not in gl.spending_by_category(OCT)


def test_an_edited_expense_counts_once_at_its_new_amount():
    gl = make()
    original = spend(gl, "120.00")
    void(gl, original)                       # the reversal an edit posts
    spend(gl, "99.00")                       # the corrected entry
    assert figures(gl) == (D("99.00"), D("99.00"), D("99.00"))


def test_a_refund_lowers_spending():
    gl = make()
    spend(gl, "120.00")
    post(gl, "2026-10-12", "Refund", [("Checking", "20.00", "0"), ("Groceries Expense", "0", "20.00")])
    assert figures(gl) == (D("100.00"), D("100.00"), D("100.00"))


def test_a_refund_in_a_later_month_is_negative_spending_not_a_chart_slice():
    gl = make()
    spend(gl, "120.00")
    post(gl, "2026-11-03", "Refund", [("Checking", "20.00", "0"), ("Groceries Expense", "0", "20.00")])
    nov = figures(gl, month="2026-11")
    assert nov[0] == D("-20.00") and nov[1] == D("-20.00")
    assert "Groceries Expense" not in gl.spending_by_category("2026-11")     # no negative slice
    assert gl.monthly_income_expense_report(2026, 11)["expense_by_account"]["Groceries Expense"] == D("-20.00")


def test_report_total_is_the_sum_of_its_accounts():
    gl = make()
    spend(gl, "50.00")
    spend(gl, "30.00", account="Dining Expense")
    void(gl, spend(gl, "99.00", account="Dining Expense"))
    report = gl.monthly_income_expense_report(2026, 10)
    assert report["expense_total"] == sum(report["expense_by_account"].values(), ZERO) == D("80.00")
    assert sum(gl.spending_by_category(OCT).values(), ZERO) == D("80.00")


def test_income_nets_like_the_revenue_helper_already_does():
    gl = make()
    pay = post(gl, "2026-10-15", "Pay", [("Checking", "3000.00", "0"), ("Salary", "0", "3000.00")])
    void(gl, pay)
    post(gl, "2026-10-16", "Pay", [("Checking", "2800.00", "0"), ("Salary", "0", "2800.00")])
    report = gl.monthly_income_expense_report(2026, 10)
    assert report["income_total"] == gl.get_total_revenue_for_month(OCT) == D("2800.00")
    assert report["net"] == D("2800.00")


def test_zero_net_accounts_are_left_out_of_the_by_account_maps():
    gl = make()
    void(gl, post(gl, "2026-10-15", "Pay", [("Checking", "3000.00", "0"), ("Salary", "0", "3000.00")]))
    assert gl.monthly_income_expense_report(2026, 10)["income_by_account"] == {}


def test_medical_total_ignores_voided_and_counts_refunds():
    gl = make()
    kept = spend(gl, "200.00", account="Medical Expense")
    void(gl, spend(gl, "75.00", account="Medical Expense"))
    post(gl, "2026-10-20", "Refund", [("Checking", "25.00", "0"), ("Medical Expense", "0", "25.00")])
    assert kept is not None
    assert gl.medical_expenses_total("2026-01-01", "2026-12-31", ["Medical Expense"]) == D("175.00")


def test_envelope_available_follows_the_corrected_spending():
    gl = make()
    void(gl, spend(gl, "100.00"))
    assert budget_engine.envelope_available(gl, "Groceries", OCT) == D("300.00")   # void: nothing spent
    spend(gl, "40.00")
    assert budget_engine.envelope_available(gl, "Groceries", OCT) == D("260.00")
    post(gl, "2026-10-20", "Refund", [("Checking", "15.00", "0"), ("Groceries Expense", "0", "15.00")])
    assert budget_engine.envelope_available(gl, "Groceries", OCT) == D("275.00")   # refund refills it
