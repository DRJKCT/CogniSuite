import ast, types
from decimal import Decimal as D, ROUND_HALF_UP
from typing import Dict, Any, List

def load_fn(path):
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "aggregate_by_category":
            code = ast.get_source_segment(src, node)
            import textwrap
            ns = {"Dict": Dict, "Any": Any, "List": List, "Decimal": D, "ZERO": D("0.00"),
                  "money": lambda v: D(v).quantize(D("0.01"), rounding=ROUND_HALF_UP)}
            exec(textwrap.dedent(code), ns)
            return ns["aggregate_by_category"]

class Acc:
    NORMAL_BALANCES = {"Revenue": "CR", "Expense": "DR", "Asset": "DR"}
    def __init__(self, t): self.account_type = t
Line = types.SimpleNamespace
def fake_ledger(lines):
    gl = types.SimpleNamespace()
    gl.accounts = {"Realized Gains — Investments": Acc("Revenue"), "Realized Gains — Crypto": Acc("Revenue"),
                   "Realized Losses — Investments": Acc("Expense"), "Realized Losses — Crypto": Acc("Expense")}
    gl.get_transactions = lambda start_date, end_date: [types.SimpleNamespace(lines=lines)]
    return gl

RG, RGC = "Realized Gains — Investments", "Realized Gains — Crypto"
RL, RLC = "Realized Losses — Investments", "Realized Losses — Crypto"
lines = [Line(account_name=RG, dr=D(0), cr=D(1000)), Line(account_name=RGC, dr=D(0), cr=D(200)),
         Line(account_name=RL, dr=D(400), cr=D(0)), Line(account_name=RLC, dr=D(100), cr=D(0))]
plain = {"capital_gains": [RG, RGC, RL, RLC]}
signed = {"capital_gains": [RG, RGC, {"account": RL, "sign": -1}, {"account": RLC, "sign": -1}]}

old = load_fn("ledger.orig.py"); new = load_fn("ledger.py")
gl = fake_ledger(lines)
assert old(gl, "2026-01-01", "2026-12-31", plain)["capital_gains"] == D("1700.00"), "original adds losses to gains"
assert new(gl, "2026-01-01", "2026-12-31", plain)["capital_gains"] == D("1700.00"), "unsigned maps behave exactly as before"
assert new(gl, "2026-01-01", "2026-12-31", signed)["capital_gains"] == D("700.00"), "signed losses net against gains"
assert new(gl, "2026-01-01", "2026-12-31", {"capital_gains": [{"account": RG}]})["capital_gains"] == D("1000.00"), "dict entry without sign defaults to +1"
for bad in (0, 2, "1", True, None):
    try: new(gl, "2026-01-01", "2026-12-31", {"capital_gains": [{"account": RL, "sign": bad}]})
    except ValueError as e: assert "must be 1 or -1" in str(e)
    else: raise AssertionError(f"sign {bad!r} should be rejected")
print("SIGN TESTS PASSED")
