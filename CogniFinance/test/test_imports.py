import os, sys, tempfile
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from decimal import Decimal as D
from PyQt6.QtWidgets import QApplication, QWidget
app = QApplication([])
import ledger
from ledger import GeneralLedger
import ofx_import, csv_transaction_import as csvi
from currency import FxInfo

def fresh(path, base="CAD"):
    os.path.exists(path) and os.remove(path)
    g = GeneralLedger(path, "00" * 32); assert g.set_base_currency(base)[0]; return g
def ok(r, l): assert r[0], f"{l}: {r[1]}"

def ofx_text(curdef, txns, bal_start, bal_end, acctid="987654321"):
    body = "".join(
        f"<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>{d}<TRNAMT>{a}<FITID>{fid}<NAME>{n}</STMTTRN>" for d, a, fid, n in txns)
    return f"""OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX><SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS><DTSERVER>20260301<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO</STATUS><STMTRS><CURDEF>{curdef}
<BANKACCTFROM><BANKID>123<ACCTID>{acctid}<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST><DTSTART>20260101<DTEND>20260301
{body}
</BANKTRANLIST><LEDGERBAL><BALAMT>{bal_end}<DTASOF>20260301</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"""

tmp = tempfile.mkdtemp()
# ------------------------------------------------------------------ OFX: USD statement into a CAD ledger
usd_tx = [("20260105120000", "-45.20", "U1", "COFFEE SHOP"), ("20260210120000", "1000.00", "U2", "PAYMENT IN"),
          ("20260215120000", "-100.00", "U3", "GROCER")]
p_usd = os.path.join(tmp, "usd.ofx")
open(p_usd, "w").write(ofx_text("USD", usd_tx, 0, 854.80))

info = ofx_import.inspect_ofx_account(p_usd)
assert info["currency"] == "USD", info

g = fresh("/tmp/i1.db")
# no rates yet -> whole file refused, nothing created/posted
try:
    ofx_import.parse_ofx_to_ledger_transactions(p_usd, g); raise SystemExit("expected failure")
except ValueError as e:
    assert "No USD->CAD rate" in str(e) and "Nothing was imported" in str(e), e
assert len(g.transactions) == 0

ok(g.set_fx_rate("2026-01-01", "USD", "CAD", "1.30"), "r1")
ok(g.set_fx_rate("2026-02-01", "USD", "CAD", "1.40"), "r2")
txns, new_accts, _ = ofx_import.parse_ofx_to_ledger_transactions(p_usd, g)
acct = info["suggested_name"]
assert acct in g.accounts
assert g.accounts[acct].currency == "USD" and len(txns) == 3
# lines stay 3-tuples (matcher-compatible); fx rides separately; base amounts at each date's rate
assert all(len(l) == 3 for t in txns for l in t["lines"])
by_id = {t["imported_id"]: t for t in txns}
assert by_id["U1"]["fx"][acct].rate == D("1.30") and by_id["U1"]["lines"][1][2] == D("58.76")   # 45.20 x 1.30
assert by_id["U2"]["fx"][acct].rate == D("1.40") and by_id["U2"]["lines"][0][1] == D("1400.00")

# post through the real thread body (called synchronously)
th = ofx_import.OFXImportThread([p_usd], g)
res = {}
th.finished_signal.connect(lambda n, r: res.update(n=n, r=r)); th.error_signal.connect(lambda m: res.update(err=m))
th.run()
assert res.get("n") == 3 and "err" not in res, res
assert g.accounts[acct].get_native_balance() == D("854.80"), g.accounts[acct].get_native_balance()   # matches the statement
assert g.accounts[acct].get_balance() == D("1400.00") - D("58.76") - D("140.00")                      # base at posted rates
# re-importing the same file: FITID dedupe still works
th2 = ofx_import.OFXImportThread([p_usd], g); r2 = {}
th2.finished_signal.connect(lambda n, r: r2.update(n=n)); th2.run()
assert r2["n"] == 0 and len(g.transactions) == 3

# existing account, wrong currency -> refused
p_eur = os.path.join(tmp, "eur.ofx")
open(p_eur, "w").write(ofx_text("EUR", [("20260105120000", "-5.00", "E1", "X")], 0, -5.00))
try:
    ofx_import.parse_ofx_to_ledger_transactions(p_eur, g); raise SystemExit("expected mismatch error")
except ValueError as e:
    assert "EUR statement" in str(e) or "No EUR" in str(e), e

# base-currency statement: behaves exactly as before (no fx, no rates needed)
g2 = fresh("/tmp/i2.db")
p_cad = os.path.join(tmp, "cad.ofx")
open(p_cad, "w").write(ofx_text("CAD", usd_tx, 0, 854.80, acctid="111122223"))
t2, na2, _ = ofx_import.parse_ofx_to_ledger_transactions(p_cad, g2)
assert g2.accounts[na2[0][0]].currency == "CAD" and all(not t["fx"] for t in t2)
print("1. OFX: foreign statement, rates, dedupe, mismatch guard, base unchanged")

# ------------------------------------------------------------------ import_manager merge path keeps fx
import import_manager
mgr = import_manager.ImportManager(g, QWidget())
pending = g.transactions[0]
assert any(l.fx for l in pending.lines)
before_native = g.accounts[acct].get_native_balance(); before_base = g.accounts[acct].get_balance()
mgr._process_confirmed_matches([{"pending_id": pending.id, "parsed_tx": {"date": pending.date, "description": "bank text"}, "match_type": "mobile_to_bank"}])
assert g.accounts[acct].get_native_balance() == before_native and g.accounts[acct].get_balance() == before_base
assert any(t.description == "bank text" and any(l.fx for l in t.lines) for t in g.transactions)
# rejected-match repost carries fx
g3 = fresh("/tmp/i3.db"); ok(g3.set_fx_rate("2026-01-01", "USD", "CAD", "1.30"), "r")
ok(g3.create_account("USD Chk", "Asset", currency="USD"), "a")
_, fx = g3.convert_to_base("USD Chk", "10", "2026-01-05")
t = {"date": "2026-01-05", "description": "rej", "imported_id": "R1",
     "lines": [("USD Chk", D("0.00"), D("13.00")), ("Groceries", D("13.00"), D("0.00"))], "fx": {"USD Chk": fx}}
mgr3 = import_manager.ImportManager(g3, QWidget())
assert mgr3._process_rejected_matches([t], source="ofx") == 1
assert g3.accounts["USD Chk"].get_native_balance() == D("-10.00")
print("2. import manager: merge + rejected repost keep native amount and rate")

# ------------------------------------------------------------------ journal CSV round trip
src = fresh("/tmp/i4.db")
ok(src.set_fx_rate("2026-01-01", "USD", "CAD", "1.30"), "r")
ok(src.create_account("USD Sav", "Asset", currency="USD"), "a")
ok(src.record_transaction("2026-01-02", "pay", [("Checking Account", "5000", "0"), ("Salary Income", "0", "5000")]), "p")
ok(src.transfer_funds("2026-01-05", "to usd", "Checking Account", "USD Sav", "1300", to_amount="1000"), "t")
ok(src.post_simple_transaction("2026-01-06", "usd coffee", "USD Sav", "Groceries", "10"), "c")
csv_path = os.path.join(tmp, "journal.csv"); src.export_journal_csv(csv_path)
dst = fresh("/tmp/i5.db")
assert dst.import_ledger_csv(csv_path) == 3
assert dst.accounts["USD Sav"].currency == "USD"
assert dst.accounts["USD Sav"].get_native_balance() == src.accounts["USD Sav"].get_native_balance() == D("990.00")
assert dst.accounts["USD Sav"].get_balance() == src.accounts["USD Sav"].get_balance()
assert dst.accounts["Checking Account"].get_balance() == src.accounts["Checking Account"].get_balance()
wrong = fresh("/tmp/i6.db", base="USD")
try:
    wrong.import_ledger_csv(csv_path); raise SystemExit("expected base mismatch")
except ValueError as e:
    assert "base currency" in str(e)
# an old-format file (6 columns) still imports
old = os.path.join(tmp, "old.csv")
open(old, "w").write("Date,Description,Account Name,Account Type,Debit ($),Credit ($)\n2026-01-02,x,Checking Account,Asset,5.00,\n2026-01-02,x,Salary Income,Revenue,,5.00\n")
assert fresh("/tmp/i7.db").import_ledger_csv(old) == 1
print("3. journal CSV: round trip, base-mismatch guard, old format")

# ------------------------------------------------------------------ investment CSV
path = os.path.join(tmp, "trades.csv")
open(path, "w").write("Date,Symbol,Type,Quantity,Price,Fee,Currency,Rate\n"
    "2026-01-10,AAPL,Buy,10,$100.00,5,USD,1.30\n"
    "2026-01-11,SAP,Buy,2,\u20ac150.50,1,EUR,\n"
    "2026-01-12,XYZ,Buy,1,10,0,US Dollar,\n"
    "2026-01-13,XYZ,Buy,1,10,0,USD,abc\n"
    "2026-01-14,XYZ,Buy,1,10,0,,\n")
m = csvi.ColumnMapping(date_col="Date", symbol_col="Symbol", action_col="Type", quantity_col="Quantity",
                       price_col="Price", fee_col="Fee", currency_col="Currency", fx_rate_col="Rate")
r = csvi.parse_transaction_csv(path, m)
assert [(x.symbol, x.currency, x.fx_rate, x.price) for x in r.rows] == [
    ("AAPL", "USD", D("1.30"), D("100.00")), ("SAP", "EUR", None, D("150.50")), ("XYZ", None, None, D("10"))], r.rows
assert len(r.skipped) == 2 and "unrecognized currency" in r.skipped[0][1] and "invalid exchange rate" in r.skipped[1][1]
# old-style mapping (no currency columns) is unaffected
r0 = csvi.parse_transaction_csv(path, csvi.ColumnMapping("Date", "Symbol", "Type", "Quantity", "Price", fee_col="Fee"))
assert len(r0.rows) == 5 and all(x.currency is None and x.fx_rate is None for x in r0.rows)
dlg = csvi.CSVColumnMappingDialog(path)
dlg.currency_col.setText("Currency"); dlg.fx_rate_col.setText("Rate"); dlg._on_accept()
mp = dlg.get_mapping(); assert mp.currency_col == "Currency" and mp.fx_rate_col == "Rate"
print("4. investment CSV: currency/rate columns, symbols stripped, bad values skipped, old mapping unchanged")
print("ALL IMPORT TESTS PASSED")
