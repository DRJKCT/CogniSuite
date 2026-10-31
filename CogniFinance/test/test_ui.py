import os, sys, types
from unittest.mock import MagicMock
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from decimal import Decimal as D
from PyQt6.QtWidgets import QApplication, QDialog, QComboBox, QLineEdit, QTableWidget, QMessageBox, QWidget
app = QApplication([])

# config with the names ui_main wants; everything else it imports is stubbed
cfg = types.ModuleType("config"); cfg.BACKUP_DIR = "/tmp"; cfg.NOTIFICATIONS_PATH = "/tmp/n.json"; cfg.UI_CONFIG_PATH = "/tmp/u.json"
sys.modules["config"] = cfg
# settings_tab's own helpers
for name, attrs in {"price_fetch_settings": {"PriceFetchSettingsGroup": type("P", (QWidget,), {"__init__": lambda s, gl: QWidget.__init__(s)})},
                    "ui_config": {"load_section": lambda s: {}, "update_section": lambda s, v: None},
                    "simple_mode": {"load_simple_mode": lambda: False, "describe_unsimplified": lambda: ""}}.items():
    m = types.ModuleType(name); m.__dict__.update(attrs); sys.modules[name] = m
for name in ["qrcode", "html_renderers", "parser_integration", "tax_manager", "pdf_import", "notification_center",
             "notification_widgets", "log_tab", "audit_tab", "tax_tab", "investments_tab", "ofx_import",
             "TestRunnerManager", "graphs", "category_rules_tab", "backup_manager", "transaction_dialogs",
             "ui_transactions_tab", "budgets_tab", "add_edit_tab", "import_manager", "sync_tab", "reports_tab",
             "reconcile_tab", "matplotlib", "matplotlib.backends", "matplotlib.backends.backend_qtagg",
             "matplotlib.figure", "recurring_tab", "shell_config", "simple_home_tab", "simple_money_tab",
             "simple_plans_tab", "simple_reports_tab", "ui_helpers"]:
    sys.modules[name] = MagicMock()

import ledger, ui_main
from ui_main import BudgetApp, format_money

def ok(r, l): assert r[0], f"{l}: {r[1]}"
p = "/tmp/ui.db"; os.path.exists(p) and os.remove(p)
g = ledger.GeneralLedger(p, "00" * 32); ok(g.set_base_currency("CAD"), "base")

class Fake(QWidget):   # just enough of BudgetApp for the methods under test
    pass
f = Fake()
f.gl = g
f.accounts_table = QTableWidget()
f._accounts_table_wide = False
f.tab_rules = MagicMock(); f.tab_transactions = MagicMock(); f.tab_transactions2 = MagicMock(); f.tab_budgets = MagicMock()
f.refresh_overview = MagicMock(); f.refresh_balance_sheet = MagicMock()
for m in ("refresh_accounts_table", "refresh_account_combos", "_currency_picker", "_currency_from_picker",
          "add_new_account", "edit_selected_account", "_foreign_accounts_html", "_on_base_currency_changed"):
    setattr(f, m, getattr(BudgetApp, m).__get__(f))

# 1. base-only ledger: the table is exactly the old two-column layout
f.accounts_table.setColumnCount(2); f.refresh_accounts_table()
assert f.accounts_table.columnCount() == 2 and not f._accounts_table_wide
assert f._foreign_accounts_html() == ""

# dialogs: drive them by finding their widgets
dialogs = []
def fake_exec(self):
    dialogs.append(self)
    return QDialog.DialogCode.Accepted
QDialog.exec = fake_exec
msgs = []
QMessageBox.information = staticmethod(lambda *a, **k: msgs.append(("info", a[2])))
QMessageBox.warning = staticmethod(lambda *a, **k: msgs.append(("warn", a[2])))

def add_account(name, typ, ccy):
    orig = QDialog.exec
    def drive(self):
        self.findChildren(QLineEdit)[0].setText(name)
        combos = self.findChildren(QComboBox)
        combos[0].setCurrentText(typ)
        combos[1].lineEdit().setText(ccy) if combos[1].isEnabled() else None
        return QDialog.DialogCode.Accepted
    QDialog.exec = drive
    f.add_new_account(); QDialog.exec = orig

# 2. add dialog: foreign asset, base asset, and an Expense that can't be foreign
add_account("USD Savings", "Asset", "usd")
assert g.accounts["USD Savings"].currency == "USD"
add_account("Visa EUR", "Liability", "EUR"); assert g.accounts["Visa EUR"].currency == "EUR"
add_account("Dining", "Expense", "USD");     assert g.accounts["Dining"].currency == "CAD"   # picker disabled -> base
add_account("Bad", "Asset", "US");            assert "Bad" not in g.accounts and msgs[-1][0] == "warn"
add_account("Plain Asset", "Asset", "CAD");  assert g.accounts["Plain Asset"].currency == "CAD"

# 3. table now shows currency + balance columns
ok(g.set_fx_rate("2026-01-01", "USD", "CAD", "1.30"), "rate")
ok(g.record_transaction("2026-01-02", "pay", [("Checking Account", "5000", "0"), ("Salary Income", "0", "5000")]), "pay")
ok(g.transfer_funds("2026-01-05", "to usd", "Checking Account", "USD Savings", "1300", to_amount="1000"), "xfer")
f.refresh_accounts_table()
t = f.accounts_table
assert t.columnCount() == 4 and f._accounts_table_wide
rows = {t.item(r, 0).text(): [t.item(r, c).text() for c in range(4)] for r in range(t.rowCount())}
assert rows["USD Savings"][2:] == ["USD", "1,000.00 USD"], rows["USD Savings"]
assert rows["Checking Account"][2:] == ["CAD", "3,700.00 CAD"], rows["Checking Account"]
assert "1,300.00 CAD" in t.item([r for r in range(t.rowCount()) if t.item(r, 0).text() == "USD Savings"][0], 3).toolTip() if False else True
usd_row = [r for r in range(t.rowCount()) if t.item(r, 0).text() == "USD Savings"][0]
assert "1,300.00 CAD" in t.item(usd_row, 3).toolTip()

# 4. balance sheet block
h = f._foreign_accounts_html()
assert "USD Savings" in h and "1,000.00 USD" in h and "1,300.00 CAD" in h and "not revalued" in h

# 5. edit dialog: currency locked once the account has entries; free before that
def edit(name, set_ccy=None, new_type=None):
    for r in range(t.rowCount()):
        if t.item(r, 0).text() == name: t.setCurrentCell(r, 0); t.selectRow(r)
    orig = QDialog.exec
    state = {}
    def drive(self):
        combos = self.findChildren(QComboBox)
        state["enabled"] = combos[1].isEnabled()
        if new_type: combos[0].setCurrentText(new_type)
        if set_ccy and combos[1].isEnabled(): combos[1].lineEdit().setText(set_ccy)
        return QDialog.DialogCode.Accepted
    QDialog.exec = drive; f.edit_selected_account(); QDialog.exec = orig
    return state
f.gl.update_account = g.update_account
st = edit("USD Savings", set_ccy="EUR"); assert st["enabled"] is False and g.accounts["USD Savings"].currency == "USD"
st = edit("Visa EUR", set_ccy="GBP");    assert st["enabled"] is True and g.accounts["Visa EUR"].currency == "GBP"
st = edit("Visa EUR", new_type="Expense"); assert g.accounts["Visa EUR"].currency == "CAD"   # non-capable type -> base

# 6. refresh_account_combos exists and calls the other tabs' hooks
f.tab_budgets.refresh_account_dropdown.reset_mock(); f.refresh_account_combos()
assert f.tab_budgets.refresh_account_dropdown.called and f.tab_transactions2.refresh_account_dropdown.called

# 7. settings tab -> app refresh wiring
f._on_base_currency_changed("EUR"); assert f.refresh_overview.called and f.refresh_balance_sheet.called
src = open("ui_main.py").read()
assert "base_currency_changed.connect(self._on_base_currency_changed)" in src
print("UI TESTS PASSED")
