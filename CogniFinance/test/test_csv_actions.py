import sys, types, csv, tempfile, os
from decimal import Decimal as D

# --- PyQt6 stand-ins so the pure parser can be imported headlessly
def _mod(name, **attrs):
    m = types.ModuleType(name); m.__dict__.update(attrs); sys.modules[name] = m; return m
class _Stub:
    def __init__(self, *a, **k): pass
_mod("PyQt6")
_mod("PyQt6.QtCore", QThread=_Stub, pyqtSignal=lambda *a, **k: None)
_mod("PyQt6.QtWidgets", **{n: _Stub for n in ("QDialog","QVBoxLayout","QFormLayout","QLineEdit","QLabel","QDialogButtonBox","QComboBox")})

import csv_transaction_import as ci
from csv_transaction_import import ColumnMapping, parse_transaction_csv, parse_split_ratio, order_rows_for_posting

# ---- split ratio parsing
P = parse_split_ratio
assert P("2:1") == (D(2), D(1)) and P("2/1") == (D(2), D(1)) and P("2-for-1") == (D(2), D(1))
assert P("2 for 1") == (D(2), D(1)) and P("3 FOR 2") == (D(3), D(2)) and P("1:10") == (D(1), D(10))
assert P("1.5:1") == (D("1.5"), D(1)) and P("2") == (D(2), D(1))
for bad in ("2-1", "1:1", "0:1", "2:0", "", None, "abc", "2:", "-2:1"): assert P(bad) is None, bad

# ---- aliases: the double-count trap stays unaliased
a = ci.DEFAULT_ACTION_ALIASES
assert "reinvestment" not in a and "reinvest" not in a
assert a["drip"] == a["dividend reinvested"] == "drip" and a["stock split"] == "split"
assert a["staking reward"] == "staking" and a["airdrop"] == "airdrop"
assert set(a.values()) <= set(ci.CANONICAL_ACTIONS)

def write(rows, header):
    p = tempfile.mktemp(suffix=".csv")
    with open(p, "w", newline="") as f:
        w = csv.writer(f); w.writerow(header); w.writerows(rows)
    return p

H = ["Date", "Symbol", "Type", "Quantity", "Price", "Amount", "Fee", "Ratio"]
M = ColumnMapping("Date", "Symbol", "Type", "Quantity", "Price", "Amount", "Fee", ratio_col="Ratio")
rows = [  # deliberately newest-first, like many exports
    ["2024-09-01", "abc", "Sell",              "10",  "30",  "",       "",  ""],
    ["2024-06-01", "abc", "Stock Split",       "",    "",    "",       "",  "2:1"],
    ["2024-06-01", "abc", "Sell",              "5",   "26",  "",       "",  ""],
    ["2024-05-01", "abc", "DRIP",              "2",   "",    "-$30.00","",  ""],          # negative cash, amount col
    ["2024-04-01", "abc", "Dividend Reinvested","1",  "15",  "",       "",  ""],          # per-unit price only
    ["2024-03-01", "eth", "Staking Reward",    "0.5", "2400","",       "",  ""],
    ["2024-03-02", "eth", "Airdrop",           "10",  "",    "",       "",  ""],          # no price -> skipped
    ["2024-03-03", "eth", "reward",            "1",   "0",   "",       "",  ""],          # zero price -> skipped
    ["2024-02-01", "abc", "Split",             "",    "",    "",       "",  "banana"],    # bad ratio -> skipped
    ["2024-01-02", "abc", "Reinvestment",      "1",   "10",  "",       "",  ""],          # unaliased -> skipped
    ["2024-01-01", "abc", "Buy",               "100", "10",  "",       "1", ""],
    ["2024-05-02", "abc", "DRIP",              "0",   "",    "30",     "",  ""],          # zero qty -> skipped
]
res = parse_transaction_csv(write(rows, H), M)
by = {(r.date, r.action): r for r in res.rows}
assert len(res.rows) == 7, [(r.date, r.action) for r in res.rows]
assert by[("2024-05-01", "drip")].amount == D("30.00") and by[("2024-05-01", "drip")].quantity == D(2)
assert by[("2024-04-01", "drip")].amount == D(15), "price x qty fallback"
s = by[("2024-06-01", "split")]; assert (s.split_new, s.split_old) == (D(2), D(1))
assert by[("2024-03-01", "staking")].price == D(2400)
reasons = " | ".join(r for _, r in res.skipped)
for needle in ("positive fair-market", "unparseable split ratio 'banana'", "unrecognized action 'reinvestment'", "positive quantity"):
    assert needle in reasons, (needle, reasons)
assert len(res.skipped) == 5, res.skipped

# split without a ratio column mapped is skipped with a pointer to the form
M2 = ColumnMapping("Date", "Symbol", "Type", "Quantity", "Price")
r2 = parse_transaction_csv(write([["2024-06-01", "abc", "Split", "", ""]], H[:5]), M2)
assert not r2.rows and "split-ratio column" in r2.skipped[0][1]

# ---- chronological ordering: split before same-day sell; buy before sell
ordered = [(r.date, r.action) for r in order_rows_for_posting(res.rows)]
assert ordered == sorted(ordered, key=lambda x: x[0]), ordered
assert ordered.index(("2024-06-01", "split")) < ordered.index(("2024-06-01", "sell"))

# ---- end to end through the real engines, using the same poster logic as the tab
import investment_engine as ie, crypto_engine as ce
from test_corp_actions_fake import FakeGL
from lot_matcher import *  # real matcher
gl = FakeGL(); gl.create_account("Cash", "Asset", "Cash and Cash Equivalents")
inv, cry = ie.InvestmentEngine(gl), ce.CryptoEngine(gl)
cry.add_wallet("Ledger", "wallet")

def inv_poster(row):                                      # mirrors investments_tab.py
    if row.action in ("staking", "reward", "airdrop"): return False, "crypto only"
    if row.action == "split": return inv.record_stock_split(row.symbol, row.date, row.split_new, row.split_old)
    if inv.get_holding(row.symbol) is None:
        ok, m = inv.add_holding(row.symbol, f"Investment: {row.symbol}", "Investment — Equities")
        if not ok: return ok, m
    if row.action == "buy": return inv.buy(row.symbol, row.date, row.quantity, row.price, "Cash", fees=row.fee)
    if row.action == "sell": return inv.sell(row.symbol, row.date, row.quantity, row.price, "Cash", fees=row.fee)
    if row.action == "dividend": return inv.record_dividend(row.symbol, row.date, row.price, "Cash")
    if row.action == "drip": return inv.record_dividend_reinvestment(row.symbol, row.date, row.amount, row.quantity)
    return False, "unsupported"
def cry_poster(row):
    if row.action in ("drip", "split"): return False, "unsupported"
    if cry.get_holding(row.symbol, "Ledger") is None:
        ok, m = cry.add_holding(row.symbol, "Ledger", f"Crypto: {row.symbol} (Ledger)", "Crypto — Held")
        if not ok: return ok, m
    if row.action in ("staking", "reward", "airdrop"):
        return cry.record_staking_reward(row.symbol, "Ledger", row.date, row.quantity, row.price, kind=row.action)
    return False, "unsupported"

results = {}
for r in order_rows_for_posting(res.rows):
    poster = cry_poster if r.symbol == "ETH" else inv_poster
    results[(r.date, r.action)] = poster(r)
assert all(ok for ok, _ in results.values()), results
# 100 @10 (+$1 fee) -> drip 1@15 (4/1) -> drip 2 @ 15 (5/1) -> 2:1 split (6/1) -> sells
lots = [(l.date_acquired, l.quantity) for l in inv.get_open_lots("ABC")]
assert inv.get_quantity_held("ABC") == (D(100) + 1 + 2) * 2 - 5 - 10, inv.get_quantity_held("ABC")
assert [(g["cost_basis"]) for g in inv.get_realized_gains()][0] > 0
assert sum(l.quantity for l in cry.get_open_lots("ETH", "Ledger")) == D("0.5")

# ---- duplicate split guard: importing the same file twice must NOT compound
ok, m = inv.record_stock_split("ABC", "2024-06-01", D(2), D(1)); assert not ok and "already recorded" in m, m
# ...but undoing the split clears the way (LIFO: sells first)
print("ALL CSV TESTS PASSED")
