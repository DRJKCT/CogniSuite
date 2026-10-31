import os, sys
from decimal import Decimal as D
sys.path.insert(0, "old")
import ledger
from ledger import GeneralLedger
import investment_engine as ie_new, crypto_engine as ce_new
import investment_engine_old as ie_old, crypto_engine_old as ce_old

def fresh(path, base="CAD"):
    os.path.exists(path) and os.remove(path)
    g = GeneralLedger(path, "00" * 32)
    assert g.set_base_currency(base)[0]
    return g

def q(g, sql, a=()):
    conn, cur = g._connect(); cur.execute(sql, a); r = cur.fetchall(); conn.close(); return r

def ok(res, label):
    assert res[0], f"{label}: {res[1]}"
def bad(res, label, frag=""):
    assert not res[0] and frag in res[1], f"{label}: {res}"

# ------------------------------------------------------------------ 1. base-only regression
def run_base(IE, CE, path):
    g = fresh(path)
    ie, ce = IE.InvestmentEngine(g), CE.CryptoEngine(g)
    ok(g.record_transaction("2026-01-01", "fund", [("Checking Account", "50000", "0"), ("Salary Income", "0", "50000")]), "fund")
    ok(ie.add_holding("ABC", "ABC Holding", "Investment — Equities"), "h")
    ok(ie.buy("ABC", "2026-01-05", D("10"), D("100.333"), "Checking Account", fees=D("4.95")), "buy1")
    ok(ie.buy("ABC", "2026-02-05", D("5"), D("120"), "Checking Account"), "buy2")
    ok(ie.sell("ABC", "2026-03-01", D("12"), D("130.55"), "Checking Account", fees=D("4.95")), "sell")
    ok(ie.record_dividend("ABC", "2026-03-15", D("12.34"), "Checking Account"), "div")
    ok(ie.record_dividend_reinvestment("ABC", "2026-03-20", D("20.00"), D("0.15")), "drip")
    ok(ie.void_last_transaction("ABC"), "undo drip")
    ok(ce.add_wallet("Main"), "w")
    ok(ce.add_holding("BTC", "Main", "BTC Holding", "Crypto — Held"), "ch")
    ok(ce.buy("BTC", "Main", "2026-01-10", D("0.5"), D("43210.123"), "Checking Account", fees=D("9.99")), "cbuy")
    ok(ce.sell("BTC", "Main", "2026-04-01", D("0.2"), D("60000"), "Checking Account"), "csell")
    ok(ce.record_staking_reward("BTC", "Main", "2026-04-05", D("0.001"), D("61000")), "stake")
    ok(ce.void_last_transaction("BTC", "Main"), "undo stake")
    snap = {
        "lots_i": q(g, "SELECT date_acquired, quantity, cost_basis_per_unit FROM sql_investment_lots ORDER BY id"),
        "real_i": q(g, "SELECT date, quantity, cost_basis, proceeds, realized_gain FROM sql_investment_realized ORDER BY id"),
        "lots_c": q(g, "SELECT date_acquired, quantity, cost_basis_per_unit FROM sql_crypto_lots ORDER BY id"),
        "real_c": q(g, "SELECT date, quantity, cost_basis, proceeds, realized_gain FROM sql_crypto_realized ORDER BY id"),
        "bal": {n: str(a.get_balance()) for n, a in g.accounts.items()},
        "lines": q(g, "SELECT account_name, dr, cr FROM sql_transaction_lines ORDER BY id"),
    }
    return snap

a = run_base(ie_old, ce_old, "/tmp/e_old.db")
b = run_base(ie_new, ce_new, "/tmp/e_new.db")
assert a == b, [k for k in a if a[k] != b[k]]
print("1. base-currency behaviour identical to the original engines")

# ------------------------------------------------------------------ 2. foreign cash account
g = fresh("/tmp/e_fx.db")
ie, ce = ie_new.InvestmentEngine(g), ce_new.CryptoEngine(g)
ok(g.create_account("USD Brokerage", "Asset", currency="USD"), "usd acct")
ok(g.record_transaction("2026-01-01", "fund", [("Checking Account", "20000", "0"), ("Salary Income", "0", "20000")]), "fund")
ok(g.set_fx_rate("2026-01-01", "USD", "CAD", "1.30"), "r1")
ok(g.set_fx_rate("2026-03-01", "USD", "CAD", "1.40"), "r2")
ok(g.transfer_funds("2026-01-02", "to usd", "Checking Account", "USD Brokerage", "13000", to_amount="10000"), "fund usd")
assert g.accounts["USD Brokerage"].get_native_balance() == D("10000.00")

ok(ie.add_holding("AAPL", "AAPL Holding", "Investment — Equities"), "h")
assert g.accounts["AAPL Holding"].currency == "CAD"          # holdings stay in base

# buy 10 @ 100 USD + 5 USD fee, Jan rate 1.30 -> 1005 USD -> 1306.50 CAD
ok(ie.buy("AAPL", "2026-01-10", D("10"), D("100"), "USD Brokerage", fees=D("5")), "usd buy")
lots = ie.get_open_lots("AAPL")
assert len(lots) == 1 and lots[0].cost_basis_per_unit * 10 == D("1306.50"), lots
assert g.accounts["USD Brokerage"].get_native_balance() == D("8995.00"), g.accounts["USD Brokerage"].get_native_balance()
assert g.accounts["AAPL Holding"].get_balance() == D("1306.50")

# sell 10 @ 120 USD, fee 5 USD on Mar rate 1.40 -> net 1195 USD -> 1673.00 CAD; gain 366.50
ok(ie.sell("AAPL", "2026-03-05", D("10"), D("120"), "USD Brokerage", fees=D("5")), "usd sell")
rg = ie.get_realized_gains()
assert rg[-1]["proceeds"] == D("1673.00") and rg[-1]["cost_basis"] == D("1306.50") and rg[-1]["realized_gain"] == D("366.50"), rg[-1]
assert g.accounts["USD Brokerage"].get_native_balance() == D("10190.00")
assert g.accounts["Realized Gains — Investments"].get_balance() == D("366.50")
line = [l for l in g.get_last_transaction().lines if l.account_name == "USD Brokerage"][0]
assert line.fx.native == D("1195.00") and line.fx.rate == D("1.40") and line.fx.rate_date == "2026-03-01"

# undo the sale: native + base both restored exactly, lot returns
ok(ie.void_last_transaction("AAPL"), "undo sell")
assert g.accounts["USD Brokerage"].get_native_balance() == D("8995.00")
assert g.accounts["Realized Gains — Investments"].get_balance() == D("0.00")
assert ie.get_quantity_held("AAPL") == D("10")

# foreign dividend + DRIP in USD
ok(ie.record_dividend("AAPL", "2026-03-10", D("50"), "USD Brokerage"), "usd div")
assert g.accounts["Dividend Income — Investments"].get_balance() == D("70.00")
assert g.accounts["USD Brokerage"].get_native_balance() == D("9045.00")
ok(ie.void_last_transaction("AAPL"), "undo div")
assert g.accounts["USD Brokerage"].get_native_balance() == D("8995.00")
ok(ie.record_dividend_reinvestment("AAPL", "2026-03-12", D("40"), D("0.3"), currency="USD"), "usd drip")
assert g.accounts["Dividend Income — Investments"].get_balance() == D("56.00")
bad(ie.record_dividend_reinvestment("AAPL", "2026-03-12", D("40"), D("0.3"), currency="EUR"), "no eur rate", "No EUR->CAD")

# explicit rate override
ok(ie.buy("AAPL", "2026-03-20", D("1"), D("100"), "USD Brokerage", fx_rate="1.50"), "explicit rate")
assert ie.get_open_lots("AAPL")[-1].cost_basis_per_unit == D("150.00")

# missing rate -> nothing posted, lots unchanged
ok(g.create_account("EUR Cash", "Asset", currency="EUR"), "eur")
before = (len(g.transactions), len(ie.get_open_lots("AAPL")))
bad(ie.buy("AAPL", "2026-03-21", D("1"), D("10"), "EUR Cash"), "no rate", "No EUR->CAD")
assert (len(g.transactions), len(ie.get_open_lots("AAPL"))) == before

# ------------------------------------------------------------------ 3. crypto
ok(ce.add_wallet("Exch"), "w")
ok(ce.add_holding("ETH", "Exch", "ETH Holding", "Crypto — Held"), "ch")
ok(ce.buy("ETH", "Exch", "2026-01-15", D("2"), D("2000"), "USD Brokerage"), "eth buy")          # 4000 USD @1.30 = 5200 CAD
assert ce.get_open_lots("ETH", "Exch")[0].cost_basis_per_unit == D("2600.00")
ok(ce.sell("ETH", "Exch", "2026-03-10", D("1"), D("2500"), "USD Brokerage"), "eth sell")      # 2500 USD @1.40 = 3500; cost 2600; gain 900
cg = ce.get_realized_gains()[-1]
assert cg["proceeds"] == D("3500.00") and cg["realized_gain"] == D("900.00"), cg
ok(ce.void_last_transaction("ETH", "Exch"), "undo eth sell")
ok(ce.record_staking_reward("ETH", "Exch", "2026-03-11", D("0.01"), D("2500"), currency="USD"), "usd stake")  # 25 USD @1.40 = 35.00
assert g.accounts[ce_new.STAKING_INCOME_ACCOUNT].get_balance() == D("35.00"), g.accounts[ce_new.STAKING_INCOME_ACCOUNT].get_balance()
ok(ce.void_last_transaction("ETH", "Exch"), "undo stake")

# ------------------------------------------------------------------ 4. books still balance, history still verifies
tot_dr = sum(a.get_balance() for a in g.accounts.values() if a.NORMAL_BALANCES[a.account_type] == "DR")
tot_cr = sum(a.get_balance() for a in g.accounts.values() if a.NORMAL_BALANCES[a.account_type] == "CR")
assert tot_dr == tot_cr, (tot_dr, tot_cr)
print("verify:", g.verify_audit_chain_integrity())
print("ENGINE FX TESTS PASSED")
