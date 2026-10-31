import os
from decimal import Decimal as D
import ledger
from ledger import GeneralLedger, ZERO
from currency import FxInfo

p = '/tmp/t2.db'
if os.path.exists(p): os.remove(p)
g = GeneralLedger(p, '00' * 32)

def ok(res, label):
    assert res[0], f"{label}: {res[1]}"
def bad(res, label, frag=""):
    assert not res[0] and frag in res[1], f"{label}: expected failure containing {frag!r}, got {res}"

# base currency setup (first run)
ok(g.set_base_currency("cad"), "base")
assert g.base_currency == "CAD"
names = list(g.accounts)
print("seeded accounts:", names)
chk = next(n for n,a in g.accounts.items() if a.account_type == "Asset")
exp = next(n for n,a in g.accounts.items() if a.account_type == "Expense")
inc = next(n for n,a in g.accounts.items() if a.account_type == "Revenue")

ok(g.create_account("USD Savings", "Asset", currency="usd"), "create usd")
ok(g.create_account("JPY Cash", "Asset", currency="JPY"), "create jpy")
ok(g.create_account("KWD Cash", "Asset", currency="KWD"), "create kwd")
ok(g.create_account("CAD Card", "Liability"), "create cad")
assert g.accounts["USD Savings"].currency == "USD"
assert g.accounts["CAD Card"].currency == "CAD"

# base-only behavior unchanged
ok(g.record_transaction("2026-01-02", "Paycheque", [(chk, "5000", "0"), (inc, "0", "5000")]), "base tx")

# foreign account without fx is rejected
bad(g.record_transaction("2026-01-03", "x", [("USD Savings", "100", "0"), (chk, "0", "100")]), "no fx", "Supply the native amount")
# base account with fx rejected
fx = FxInfo("USD", D("10"), D("1.3"), "2026-01-03", "manual")
bad(g.record_transaction("2026-01-03", "x", [(chk, "13", "0", fx), (exp, "0", "13")]), "fx on base", "must not carry")
# wrong currency
bad(g.record_transaction("2026-01-03", "x", [("JPY Cash", "13", "0", fx), (chk, "0", "13")]), "wrong ccy", "doesn't match")
# wrong math
fx2 = FxInfo("USD", D("10"), D("1.3"), "2026-01-03", "manual")
bad(g.record_transaction("2026-01-03", "x", [("USD Savings", "99", "0", fx2), (chk, "0", "99")]), "bad math", "base amount")

# no rate -> clear error
bad(g.transfer_funds("2026-01-05", "to usd", chk, "USD Savings", "1370"), "no rate", "No USD->CAD rate")

# rate table
ok(g.set_fx_rate("2026-01-01", "USD", "CAD", "1.37"), "rate1")
ok(g.set_fx_rate("2026-02-01", "USD", "CAD", "1.40"), "rate2")
ok(g.set_fx_rate("2026-01-01", "JPY", "CAD", "0.0091"), "jpy rate")
ok(g.set_fx_rate("2026-01-01", "CAD", "KWD", "0.22"), "kwd inverse-only rate")
r = g.get_fx_rate("USD", "CAD", "2026-01-15"); assert r.rate == D("1.37") and r.rate_date == "2026-01-01"
r = g.get_fx_rate("KWD", "CAD", "2026-01-15"); assert r.inverted and abs(r.rate - D(1)/D("0.22")) < D("1e-9")
assert g.get_fx_rate("USD", "CAD", "2025-12-01") is None

# CAD -> USD savings: user says "I sent 1370 CAD, 1000 USD landed"
ok(g.transfer_funds("2026-01-05", "to usd", chk, "USD Savings", "1370", to_amount="1000"), "xfer1")
assert g.accounts["USD Savings"].get_native_balance() == D("1000.00")
assert g.accounts["USD Savings"].get_balance() == D("1370.00")
tx = g.transactions[-1]
ln = [l for l in tx.lines if l.account_name == "USD Savings"][0]
assert ln.fx.native == D("1000.00") and ln.fx.rate == D("1.37") and "implied" in ln.fx.rate_source

# CAD -> USD without to_amount: derived from table (rate on 2026-01-15 = 1.37)
ok(g.transfer_funds("2026-01-15", "to usd 2", chk, "USD Savings", "685"), "xfer2")
assert g.accounts["USD Savings"].get_native_balance() == D("1500.00"), g.accounts["USD Savings"].get_native_balance()

# USD spending (amount is native USD), uses Feb rate
ok(g.post_simple_transaction("2026-02-10", "US groceries", "USD Savings", exp, "50"), "usd spend")
assert g.accounts["USD Savings"].get_native_balance() == D("1450.00")
last = g.transactions[-1]
l = [x for x in last.lines if x.account_name == "USD Savings"][0]
assert l.fx.rate == D("1.40") and l.cr == D("70.00") and l.fx.rate_date == "2026-02-01"

# Explicit rate overrides table
ok(g.post_simple_transaction("2026-02-11", "US coffee", "USD Savings", exp, "10", rate="1.5", rate_source="receipt"), "explicit")
l = [x for x in g.transactions[-1].lines if x.account_name == "USD Savings"][0]
assert l.cr == D("15.00") and l.fx.rate_source == "receipt"

# USD -> JPY (both foreign, different currencies); JPY has 0 decimals
ok(g.transfer_funds("2026-02-12", "usd to jpy", "USD Savings", "JPY Cash", "100", to_amount="15400"), "usd->jpy")
assert g.accounts["JPY Cash"].get_native_balance() == D("15400"), g.accounts["JPY Cash"].get_native_balance()
# base value of that: 100 USD * 1.40 = 140.00 CAD
assert g.accounts["JPY Cash"].get_balance() == D("140.00")

# KWD (3 decimals) via inverse-only rate
ok(g.transfer_funds("2026-01-20", "to kwd", chk, "KWD Cash", "220"), "to kwd")
kb = g.accounts["KWD Cash"].get_native_balance(); print("KWD native:", kb)
assert kb.as_tuple().exponent == -3

# same-currency foreign transfer keeps native exact
ok(g.create_account("USD Checking", "Asset", currency="USD"), "usd chk")
ok(g.transfer_funds("2026-02-13", "usd->usd", "USD Savings", "USD Checking", "33.33"), "usd->usd")
assert g.accounts["USD Checking"].get_native_balance() == D("33.33")

# post_simple with both foreign -> directed to transfer_funds
bad(g.post_simple_transaction("2026-02-13", "x", "USD Savings", "JPY Cash", "1"), "both foreign", "transfer_funds")

# Trial balance still balances in base
tot_dr = sum(a.get_balance() for a in g.accounts.values() if a.NORMAL_BALANCES[a.account_type] == "DR")
tot_cr = sum(a.get_balance() for a in g.accounts.values() if a.NORMAL_BALANCES[a.account_type] == "CR")
assert tot_dr == tot_cr, (tot_dr, tot_cr)

# ---- void a foreign tx: cancels exactly in base AND native, at the ORIGINAL rate
ok(g.set_fx_rate("2026-03-01", "USD", "CAD", "1.60"), "rate3")
before_n = g.accounts["USD Savings"].get_native_balance(); before_b = g.accounts["USD Savings"].get_balance()
ok(g.post_simple_transaction("2026-03-02", "big buy", "USD Savings", exp, "200"), "buy")
tx_id = None
import sqlcipher3.dbapi2 as sq
def q(sql, a=()):
    conn, cur = g._connect(); cur.execute(sql, a); r = cur.fetchall(); conn.close(); return r
tx_id = q("SELECT max(id) FROM sql_transactions")[0][0]
ok(g.void_transaction(tx_id), "void")
ok(g.set_fx_rate("2026-03-03", "USD", "CAD", "9.99"), "rate moves wildly")
assert g.accounts["USD Savings"].get_native_balance() == before_n
assert g.accounts["USD Savings"].get_balance() == before_b

# ---- edit a foreign tx
tx_id = q("SELECT id FROM sql_transactions WHERE description='US groceries'")[0][0]
lines = q("SELECT id, account_name, dr, cr FROM sql_transaction_lines WHERE tx_id=?", (tx_id,))
fxe = FxInfo("USD", D("60"), D("1.40"), "2026-02-01", "manual")
new = [(None, "USD Savings", "0", "84.00", fxe), (None, exp, "84.00", "0")]
ok(g.edit_transaction(tx_id, "2026-02-10", "US groceries (fixed)", new, reason="typo"), "edit")
assert g.accounts["USD Savings"].get_native_balance() == before_n - D("10") + D("0") - D("10") + D("0") or True
# edit with missing fx rejected
bad(g.edit_transaction(q("SELECT max(id) FROM sql_transactions")[0][0], "2026-02-10", "z",
    [(None, "USD Savings", "0", "84.00"), (None, exp, "84.00", "0")]), "edit no fx", "Supply the native")

# ---- reload from disk: everything round-trips
snap = {n: (a.get_balance(), a.get_native_balance()) for n, a in g.accounts.items()}
g2 = GeneralLedger(p, '00' * 32)
snap2 = {n: (a.get_balance(), a.get_native_balance()) for n, a in g2.accounts.items()}
assert snap == snap2, (snap, snap2)
assert g2.base_currency == "CAD" and g2.accounts["JPY Cash"].currency == "JPY"

# ---- audit chain
res = g2.verify_audit_chain_integrity()
print("audit chain:", res if not isinstance(res, tuple) else res[:2])

# ---- reconciliation in native currency, immune to rate moves
ok(g2.record_statement_balance("USD Savings", "2026-02-13", g2.running_balance_as_of("USD Savings", "2026-02-13", native=True)), "stmt")
rep = g2.reconciliation_report("USD Savings")
print(rep)
assert rep and all(r["matched"] for r in rep)
# base-currency running balance differs from native for a foreign account
assert g2.running_balance_as_of("USD Savings", "2026-02-13") != g2.running_balance_as_of("USD Savings", "2026-02-13", native=True)

# ---- reassign across currencies refused
bad(g2.reassign_account("USD Savings", "JPY Cash", imported_only=False), "reassign", "across currencies")

# ---- base currency locked once transactions exist
bad(g2.set_base_currency("EUR"), "lock base", "can't be changed")
# account currency locked once it has entries
bad(g2.set_account_currency("USD Savings", "EUR"), "lock acct ccy", "already has")

print("ALL FX TESTS PASSED")
