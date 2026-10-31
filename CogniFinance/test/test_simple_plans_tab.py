"""
test_simple_plans_tab.py

The Plans screen and its dialogs: the right month, the engine's numbers,
honest wording about carried-over money, and every action going through
Pro's own ledger methods.
"""
import os
import sys
from datetime import datetime
from decimal import Decimal

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication, QLabel, QPushButton

import budget_engine
import budget_summary
import plans_dialogs
import simple_plans_tab
from test_budget_summary import FakeLedger, FakeAccount

D = Decimal
NOW = datetime(2026, 10, 25, 9, 0)


class PlansFake(FakeLedger):
    def __init__(self):
        super().__init__()
        self.calls, self.result, self.income_plan = [], (True, "ok"), None

    def assign_to_envelope(self, month, category, amount, note=None):
        self.calls.append(("assign", month, category, amount)); return self.result

    def unassign_from_envelope(self, month, category, amount, note=None):
        self.calls.append(("unassign", month, category, amount)); return self.result

    def move_budget_money(self, from_category, to_category, amount, month, note=None):
        self.calls.append(("move", from_category, to_category, amount, month)); return self.result

    def upsert_budget_envelope(self, category, linked, policy, is_active):
        self.calls.append(("upsert", category, linked, policy, is_active)); return self.result

    def get_income_plan(self, month):
        return self.income_plan


def make():
    gl = PlansFake()
    for name in ("Groceries Expense", "Dining Expense", "Fun Expense", "Free Expense"):
        gl.accounts[name] = FakeAccount("Expense")
    gl.add_envelope("Groceries", "Groceries Expense", "carry_forward")
    gl.add_envelope("Dining", "Dining Expense", "reset_monthly")
    gl.add_envelope("Fun", None, "carry_forward")
    gl.set_revenue("2026-10", "3200")
    gl.add_row("2026-09", "Groceries", "assign", "400")
    gl.set_spent("Groceries Expense", "2026-09", "430")          # 30 over, carried
    gl.add_row("2026-10", "Groceries", "assign", "300")
    gl.set_spent("Groceries Expense", "2026-10", "250")          # 20 left
    gl.add_row("2026-10", "Dining", "assign", "100")
    gl.set_spent("Dining Expense", "2026-10", "130")             # 30 over
    return gl


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def gl():
    return make()


@pytest.fixture
def tab(app, gl):
    return simple_plans_tab.SimplePlansTab(gl, now_fn=lambda: NOW)


def texts(widget):
    return [l.text() for l in widget.findChildren(QLabel)]


def run_dialog(dialog):
    """Click the dialog's main button without opening it modally."""
    dialog.action_button.click()
    return dialog


# ------------------------------------------------------------------ screen

def test_it_shows_the_current_month_not_a_fixed_one(app, gl):
    tab = simple_plans_tab.SimplePlansTab(gl, now_fn=lambda: NOW)
    assert tab.month == "2026-10" and tab.month_label.text() == "October 2026"
    tab.next_button.click()
    assert tab.month == "2026-11" and tab.month_label.text() == "November 2026"
    tab.prev_button.click(); tab.prev_button.click()
    assert tab.month == "2026-09"


def test_left_to_assign_is_the_engines_figure(tab, gl):
    expected = budget_engine.ready_to_assign(gl, "2026-10")
    assert tab.left_amount.text() == f"${expected:,.2f}" == "$2,800.00"


def test_over_assigned_is_said_plainly(app):
    gl = make()
    gl.set_revenue("2026-10", "100")
    tab = simple_plans_tab.SimplePlansTab(gl, now_fn=lambda: NOW)
    assert "Over-assigned" in tab.left_note.text()


def test_overspent_first_and_only_they_get_a_cover_button(tab):
    names = [l.text() for l in tab.cards_host.findChildren(QLabel) if l.text() in ("Groceries", "Dining", "Fun")]
    assert names[0] == "Dining"                                   # overspent first
    covers = [b for b in tab.cards_host.findChildren(QPushButton) if b.text() == "Cover it"]
    assert len(covers) == 1


def test_carry_over_is_explained_so_the_numbers_add_up(tab):
    body = " ".join(texts(tab.cards_host))
    assert "Unspent money carries over to next month." in body
    assert "Starts $30.00 short from earlier months." in body      # Groceries began October 30 over
    assert "Resets each month." in body                            # Dining


def test_an_envelope_with_no_linked_account_says_it_tracks_nothing(tab):
    assert "Not tracking any spending" in " ".join(texts(tab.cards_host))


def test_jargon_and_developer_notes_are_not_shown(tab):
    body = " ".join(texts(tab)).lower()
    assert "rollover policy hidden" not in body and "stored" not in body and "planning layer" not in body


def test_income_plan_line(app, gl):
    gl.income_plan = {"expected_amount": D("3000.00")}
    assert "Expected this month: $3,000.00" in texts(simple_plans_tab.SimplePlansTab(gl, now_fn=lambda: NOW))
    gl.income_plan = None
    assert "No income plan set for this month." in texts(simple_plans_tab.SimplePlansTab(gl, now_fn=lambda: NOW))


def test_a_load_failure_is_shown_not_hidden_as_an_empty_plan(app, gl, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(budget_summary, "summarize", boom)
    tab = simple_plans_tab.SimplePlansTab(gl, now_fn=lambda: NOW)
    assert "could not be loaded" in tab.footer_label.text() and "database is locked" in tab.footer_label.text()
    assert not any(b.isEnabled() for b in tab.action_buttons.values())
    assert "No envelopes yet" not in " ".join(texts(tab))


def test_dark_mode_changes_the_palette(tab):
    tab.set_dark_mode(True)
    assert tab._theme["card"] == "#1F2937"


# ----------------------------------------------------------------- dialogs

def summary_for(gl):
    return budget_summary.summarize(gl, "2026-10")


def test_assign_calls_the_ledger_and_says_what_it_does(app, gl):
    d = plans_dialogs.AssignDialog(gl, summary_for(gl), "2026-10", envelope="Fun")
    d.amount_edit.setText("50")
    assert d.note.text() == "Moves $50.00 from Left to assign into Fun."
    run_dialog(d)
    assert gl.calls == [("assign", "2026-10", "Fun", D("50.00"))] and d.changed


def test_take_back_calls_unassign(app, gl):
    d = plans_dialogs.AssignDialog(gl, summary_for(gl), "2026-10", envelope="Groceries", take_back=True)
    d.amount_edit.setText("10")
    assert "Returns $10.00 from Groceries to Left to assign." == d.note.text()
    run_dialog(d)
    assert gl.calls == [("unassign", "2026-10", "Groceries", D("10.00"))]


def test_bad_amount_sends_nothing(app, gl):
    d = plans_dialogs.AssignDialog(gl, summary_for(gl), "2026-10")
    d.amount_edit.setText("zero")
    run_dialog(d)
    assert gl.calls == [] and "number" in d.status.text() and not d.changed


def test_ledger_refusal_is_shown(app, gl):
    gl.result = (False, "Amount must be positive.")
    d = plans_dialogs.AssignDialog(gl, summary_for(gl), "2026-10")
    d.amount_edit.setText("5")
    run_dialog(d)
    assert "must be positive" in d.status.text() and not d.changed


def test_move_uses_the_ledgers_atomic_move(app, gl):
    d = plans_dialogs.MoveDialog(gl, summary_for(gl), "2026-10")
    d.from_combo.setCurrentIndex(d.from_combo.findData("Groceries"))
    d.to_combo.setCurrentIndex(d.to_combo.findData("Dining"))
    d.amount_edit.setText("15")
    assert "Left to assign does not change" in d.note.text()
    run_dialog(d)
    assert gl.calls == [("move", "Groceries", "Dining", D("15.00"), "2026-10")]


def test_move_to_the_same_envelope_is_refused_first(app, gl):
    d = plans_dialogs.MoveDialog(gl, summary_for(gl), "2026-10")
    d.from_combo.setCurrentIndex(0); d.to_combo.setCurrentIndex(0)
    d.amount_edit.setText("5")
    run_dialog(d)
    assert gl.calls == [] and "two different envelopes" in d.status.text()


def test_cover_is_prefilled_with_the_shortfall_and_offers_only_envelopes_with_money(app, gl):
    d = plans_dialogs.MoveDialog(gl, summary_for(gl), "2026-10", target="Dining")
    assert d.amount_edit.text() == "30.00"
    assert d.to_combo.currentData() == "Dining"
    sources = [d.from_combo.itemData(i) for i in range(d.from_combo.count())]
    assert sources == ["Groceries"]                 # Fun has nothing; Dining is the target
    run_dialog(d)
    assert gl.calls == [("move", "Groceries", "Dining", D("30.00"), "2026-10")]


def test_cover_with_nobody_to_cover_it_says_so(app):
    gl = make()
    gl.set_spent("Groceries Expense", "2026-10", "400")         # now Groceries is over too
    d = plans_dialogs.MoveDialog(gl, summary_for(gl), "2026-10", target="Dining")
    assert "No other envelope has money" in d.status.text() and not d.action_button.isEnabled()


def test_add_envelope_defaults_to_carry_forward_and_free_accounts_only(app, gl):
    d = plans_dialogs.AddEnvelopeDialog(gl, summary_for(gl), "2026-10")
    accounts = [d.account_combo.itemData(i) for i in range(d.account_combo.count())]
    assert accounts == ["Free Expense", "Fun Expense"]    # Groceries and Dining are already tracked
    d.name_edit.setText("Misc")
    d.account_combo.setCurrentIndex(d.account_combo.findData("Free Expense"))
    run_dialog(d)
    assert gl.calls == [("upsert", "Misc", "Free Expense", "carry_forward", True)] and d.changed


def test_add_envelope_refuses_a_duplicate_name(app, gl):
    d = plans_dialogs.AddEnvelopeDialog(gl, summary_for(gl), "2026-10")
    d.name_edit.setText("dining")
    run_dialog(d)
    assert gl.calls == [] and "already exists" in d.status.text()


def test_change_link_keeps_the_envelopes_policy(app, gl):
    d = plans_dialogs.ChangeLinkDialog(gl, summary_for(gl), "2026-10", envelope="Fun")
    d.account_combo.setCurrentIndex(d.account_combo.findData("Free Expense"))
    run_dialog(d)
    assert gl.calls == [("upsert", "Fun", "Free Expense", "carry_forward", True)]


def test_a_change_on_the_screen_refreshes_and_reports(tab, gl, monkeypatch):
    seen = []
    tab.data_modified.connect(lambda: seen.append(True))

    class Fake:
        changed = True
        def setStyleSheet(self, _): pass
        def exec(self): pass

    tab._run(Fake())
    assert seen == [True]
