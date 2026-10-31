import sqlite3, tempfile, types
from decimal import Decimal as D
import crypto_engine as ce

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
e = ce.CryptoEngine(gl)
assert gl.accounts[ce.STAKING_INCOME_ACCOUNT] == ("Revenue", "Crypto — Staking Income")
assert e.add_wallet("Ledger", "wallet")[0] and e.add_wallet("Coinbase", "exchange_account")[0]
assert e.add_holding("ETH", "Ledger", "Crypto: ETH (Ledger)", "Crypto — Held")[0]
assert e.add_holding("ETH", "Coinbase", "Crypto: ETH (Coinbase)", "Crypto — Held")[0]

def lots(w="Ledger"): return [(l.date_acquired, l.quantity, l.cost_basis_per_unit) for l in e.get_open_lots("ETH", w)]
INC = ce.STAKING_INCOME_ACCOUNT
HOLD = "Crypto: ETH (Ledger)"

ok, m = e.buy("ETH", "Ledger", "2024-01-05", D(1), D(2000), "Cash"); assert ok, m

# --- reward: income at FMV, new lot at that FMV, no cash leg
ok, m = e.record_staking_reward("ETH", "Ledger", "2024-05-01", D("0.5"), D(2400)); assert ok, m
assert gl.bal[INC] == D(-1200) and gl.bal[HOLD] == D(3200) and gl.bal["Cash"] == D(-2000)
assert lots() == [("2024-01-05", D(1), D(2000)), ("2024-05-01", D("0.5"), D(2400))], lots()
assert lots("Coinbase") == [], "other wallet untouched"

# --- sell: FIFO takes the bought lot first, then the reward lot carries its FMV basis
ok, m = e.sell("ETH", "Ledger", "2024-09-01", D("1.5"), D(3000), "Cash"); assert ok, m
g = e.get_realized_gains()[-1]
assert g["cost_basis"] == D(3200) and g["realized_gain"] == D(4500) - D(3200), g

# --- undo the sell, then the reward (strict LIFO), then buy
assert e.void_last_transaction("ETH", "Ledger")[0]
ok, m = e.void_last_transaction("ETH", "Ledger"); assert ok, m
assert gl.bal[INC] == D(0) and gl.bal[HOLD] == D(2000), (gl.bal[INC], gl.bal[HOLD])
assert lots() == [("2024-01-05", D(1), D(2000))], lots()

# --- rounding: ledger and lot agree to the cent; per-unit basis derived from rounded income
ok, m = e.record_staking_reward("ETH", "Ledger", "2024-06-01", D("0.33333333"), D("3000.123"), kind="airdrop"); assert ok, m
q, cpu = lots()[-1][1], lots()[-1][2]
income = (D("0.33333333") * D("3000.123")).quantize(D("0.01"))
assert -gl.bal[INC] == income and abs(q * cpu - income) < D("1e-20"), (gl.bal[INC], q * cpu)
assert "Airdrop" in [r for r in sqlite3.connect(gl.path).execute("SELECT description FROM sql_transactions")][-1][0]
assert e.void_last_transaction("ETH", "Ledger")[0] and gl.bal[INC] == 0

# --- validation
assert not e.record_staking_reward("ETH", "Ledger", "2024-06-01", D(1), D(0))[0]
assert not e.record_staking_reward("ETH", "Ledger", "2024-06-01", D(0), D(5))[0]
assert not e.record_staking_reward("ETH", "Ledger", "2024-06-01", D(1), D(-5))[0]
assert not e.record_staking_reward("ETH", "Ledger", "2024-06-01", D(1), D(5), kind="gift")[0]
assert not e.record_staking_reward("DOGE", "Ledger", "2024-06-01", D(1), D(5))[0]
assert not e.record_staking_reward("ETH", "Nowhere", "2024-06-01", D(1), D(5))[0]
ok, m = e.record_staking_reward("ETH", "Ledger", "2024-06-01", D("0.000001"), D("1")); assert not ok and "cent" in m, m
assert gl.bal[INC] == 0 and len(lots()) == 1, "failed validations must change nothing"

# --- reward into a second wallet keeps per-wallet lots separate
assert e.record_staking_reward("ETH", "Coinbase", "2024-07-01", D(2), D(2500))[0]
assert len(lots("Coinbase")) == 1 and len(lots()) == 1
print("ALL CRYPTO TESTS PASSED")
