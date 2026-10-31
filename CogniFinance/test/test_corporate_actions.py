import sqlite3, tempfile, os, types
from decimal import Decimal as D
import investment_engine as ie

class FakeGL:
    def __init__(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.accounts = {}
        self.txs = []
        c = sqlite3.connect(self.path)
        c.execute("CREATE TABLE sql_transactions (id INTEGER PRIMARY KEY, date TEXT, description TEXT, reconciled INT DEFAULT 0, is_voided INT DEFAULT 0)")
        c.execute("CREATE TABLE sql_transaction_lines (tx_id INT, account_name TEXT, dr TEXT, cr TEXT)")
        c.commit(); c.close()
        self.bal = {}
    def _connect(self):
        c = sqlite3.connect(self.path); return c, c.cursor()
    def create_account(self, name, t, rl): self.accounts[name] = (t, rl); self.bal.setdefault(name, D(0)); return True, ""
    def record_transaction(self, date, desc, lines):
        dr = sum((l[1] for l in lines), D(0)); cr = sum((l[2] for l in lines), D(0))
        if dr != cr: return False, f"unbalanced {dr} vs {cr}"
        c, cur = self._connect()
        cur.execute("INSERT INTO sql_transactions (date, description) VALUES (?,?)", (date, desc))
        tid = cur.lastrowid
        for a, d, k in lines:
            cur.execute("INSERT INTO sql_transaction_lines VALUES (?,?,?,?)", (tid, a, str(d), str(k)))
            self.bal[a] = self.bal.get(a, D(0)) + d - k
        c.commit(); c.close(); self.last = tid; return True, ""
    def get_last_transaction(self): return types.SimpleNamespace(id=self.last)
    def period_lock_violation(self, d, accts): return None

gl = FakeGL()
gl.create_account("Cash", "Asset", "Cash and Cash Equivalents")
e = ie.InvestmentEngine(gl)
assert e.add_holding("ABC", "Investment: ABC", "Investment — Equities")[0]

def lots(): return [(l.date_acquired, l.quantity, l.cost_basis_per_unit) for l in e.get_open_lots("ABC")]
def total_basis(): return sum(q*c for _, q, c in lots())

ok, m = e.buy("ABC", "2024-01-10", D(100), D(10), "Cash"); assert ok, m          # basis 1000
ok, m = e.buy("ABC", "2024-03-01", D(50), D(12), "Cash"); assert ok, m           # basis 600
assert total_basis() == D(1600)

# --- split: 2-for-1 effective 2024-06-01
ok, m = e.record_stock_split("ABC", "2024-06-01", D(2), D(1)); assert ok, m
assert lots() == [("2024-01-10", D(200), D(5)), ("2024-03-01", D(100), D(6))], lots()
assert total_basis() == D(1600), "split must not change total basis"
assert e.get_corporate_actions("ABC")[0]["new_shares"] == D(2)
assert gl.bal["Investment: ABC"] == D(1600), "split must not touch the ledger"

# --- post-split buy (dated after the split) is untouched by a LATER-entered split of another ratio check
ok, m = e.buy("ABC", "2024-07-01", D(10), D(8), "Cash"); assert ok, m             # basis 80
assert total_basis() == D(1680)

# --- split refused when a later sale already exists
ok, m = e.sell("ABC", "2024-08-01", D(5), D(9), "Cash"); assert ok, m
ok, m = e.record_stock_split("ABC", "2024-07-15", D(3), D(1)); assert not ok and "sale recorded" in m, m
# split refused for 1-for-1 / nonsense
assert not e.record_stock_split("ABC", "2024-06-01", D(1), D(1))[0]
assert not e.record_stock_split("ABC", "2024-06-01", D(0), D(1))[0]
assert not e.record_stock_split("NOPE", "2024-06-01", D(2), D(1))[0]

# --- LIFO undo: sell, then buy, then split must come off in reverse order
assert e.void_last_transaction("ABC")[0]            # undo the sell
assert e.void_last_transaction("ABC")[0]            # undo the 2024-07-01 buy
assert total_basis() == D(1600)
ok, m = e.void_last_transaction("ABC"); assert ok, m  # undo the SPLIT (no ledger tx)
assert lots() == [("2024-01-10", D(100), D(10)), ("2024-03-01", D(50), D(12))], lots()
assert e.get_corporate_actions("ABC") == [], "undone split must drop out of history"
assert gl.bal["Investment: ABC"] == D(1600)

# --- reverse split 1-for-4 with fractional result
ok, m = e.record_stock_split("ABC", "2024-06-01", D(1), D(4)); assert ok, m
assert lots()[1][1] == D("12.5") and total_basis() == D(1600), lots()
assert e.void_last_transaction("ABC")[0]

# --- split effective BEFORE some lots: only earlier lots adjusted
ok, m = e.record_stock_split("ABC", "2024-02-01", D(2), D(1)); assert ok, m
assert lots() == [("2024-01-10", D(200), D(5)), ("2024-03-01", D(50), D(12))], lots()
assert e.void_last_transaction("ABC")[0]

# --- split with no earlier lots
ok, m = e.record_stock_split("ABC", "2023-01-01", D(2), D(1)); assert not ok and "nothing to split" in m, m

# --- DRIP
before_income = gl.bal.get(ie.DIVIDEND_INCOME_ACCOUNT, D(0))
ok, m = e.record_dividend_reinvestment("ABC", "2024-09-30", D("30.00"), D("2.5")); assert ok, m
assert gl.bal[ie.DIVIDEND_INCOME_ACCOUNT] - before_income == D(-30), "income credited"
assert gl.bal["Investment: ABC"] == D(1630) and gl.bal["Cash"] == D(-1600), "no cash leg"
new = lots()[-1]; assert new == ("2024-09-30", D("2.5"), D(12)), new
assert total_basis() == D(1630)
assert not e.record_dividend_reinvestment("ABC", "2024-09-30", D(0), D(1))[0]
assert not e.record_dividend_reinvestment("ABC", "2024-09-30", D(5), D(0))[0]
assert not e.record_dividend_reinvestment("NOPE", "2024-09-30", D(5), D(1))[0]
ok, m = e.void_last_transaction("ABC"); assert ok, m
assert len(lots()) == 2 and gl.bal["Investment: ABC"] == D(1600) and gl.bal[ie.DIVIDEND_INCOME_ACCOUNT] == before_income

# --- DRIP then sell uses the DRIP lot's basis (FIFO, oldest first, so sell everything)
e.record_dividend_reinvestment("ABC", "2024-09-30", D("30.00"), D("2.5"))
ok, m = e.sell("ABC", "2025-01-02", D("152.5"), D(11), "Cash"); assert ok, m
g = e.get_realized_gains()[-1]; assert g["cost_basis"] == D(1630), g
assert e.get_quantity_held("ABC") == 0
print("ALL TESTS PASSED")
