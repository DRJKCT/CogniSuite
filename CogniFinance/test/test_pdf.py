import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from decimal import Decimal as D
from PyQt6.QtWidgets import QApplication
app = QApplication([])
import ledger, parser_integration, pdf_import
from currency import FxInfo

p = "/tmp/pd.db"; os.path.exists(p) and os.remove(p)
g = ledger.GeneralLedger(p, "00" * 32); assert g.set_base_currency("CAD")[0]
assert g.create_account("US Bank", "Asset", currency="USD")[0]
assert g.create_account("Credit Card CAD", "Liability")[0]
assert g.set_fx_rate("2026-01-01", "USD", "CAD", "1.30")[0]

def run(txs):
    parser_integration.TXS[:] = txs
    th = pdf_import.PDFImportThread(["a.pdf"], g); out = {}
    th.finished_signal.connect(lambda n, r: out.update(n=n, r=r)); th.error_signal.connect(lambda m: out.update(err=m))
    th.run(); assert "err" not in out, out; return out

# base-currency transaction: unchanged
base_tx = {"date": "2026-01-05", "description": "Shop", "amount": D("20.00"),
           "lines": [("Groceries", D("20.00"), D("0.00")), ("Credit Card CAD", D("0.00"), D("20.00"))],
           "contra_account": "Credit Card CAD"}
o = run([base_tx]); assert o["n"] == 1 and not o["r"]["rejected_by_ledger"]

# USD statement line: amounts are native USD; converted at the date's rate
usd_tx = {"date": "2026-01-06", "description": "US store", "amount": D("10.00"),
          "lines": [("Groceries", D("10.00"), D("0.00")), ("US Bank", D("0.00"), D("10.00"))],
          "contra_account": "US Bank", "statement_balance": D("-10.00")}
o = run([usd_tx]); assert o["n"] == 1, o
assert g.accounts["US Bank"].get_native_balance() == D("-10.00")
assert g.accounts["US Bank"].get_balance() == D("-13.00") and g.accounts["Groceries"].get_balance() == D("33.00")
assert g.get_statement_balances("US Bank")[0]["balance"] == D("-10.00")

# parser-supplied fx is honoured
fx = FxInfo("USD", D("5.00"), D("1.30"), "2026-01-01", "parser")
t3 = {"date": "2026-01-07", "description": "parser fx", "amount": D("5.00"),
      "lines": [("Groceries", D("6.50"), D("0.00")), ("US Bank", D("0.00"), D("6.50"))],
      "fx": {"US Bank": fx}, "contra_account": "US Bank"}
o = run([t3]); assert o["n"] == 1 and g.accounts["US Bank"].get_native_balance() == D("-15.00")

# no rate before the date -> only that row rejected, with a reason; others still import
early = dict(usd_tx, date="2025-06-01", description="too early")
o = run([early, dict(base_tx, description="Shop 2", date="2026-01-08")])
assert o["n"] == 1 and len(o["r"]["rejected_by_ledger"]) == 1 and "No USD->CAD rate" in o["r"]["rejected_by_ledger"][0]["reason"], o["r"]

# three-line foreign entry: refused clearly, not mis-converted
split = {"date": "2026-01-09", "description": "split", "amount": D("10.00"),
         "lines": [("Groceries", D("6.00"), D("0.00")), ("Uncategorized Expense", D("4.00"), D("0.00")), ("US Bank", D("0.00"), D("10.00"))],
         "contra_account": "US Bank"}
o = run([split]); assert o["n"] == 0 and "two-line" in o["r"]["rejected_by_ledger"][0]["reason"]
print("PDF IMPORT TESTS PASSED")
