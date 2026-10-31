import sys, json, types, ast, textwrap, tempfile, os, shutil
from decimal import Decimal as D, ROUND_HALF_UP
from typing import Dict, Any, List
m = types.ModuleType("tax_format"); m.ROUNDING_MODES = ("half_up",); m.currency_minor_units = lambda c: 2
m.round_to_increment = lambda v, inc=D("0.01"), mode="half_up": D(v).quantize(D("0.01"), rounding=ROUND_HALF_UP)
sys.modules["tax_format"] = m
l = types.ModuleType("ledger"); l.ZERO = D("0.00"); sys.modules["ledger"] = l
import tax_parser as tp, tax_calculator as tc

# --- real signed aggregator, extracted from the patched ledger.py
src = open("/home/claude/l/ledger.py", encoding="utf-8").read()
node = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "aggregate_by_category")
ns = {"Dict": Dict, "Any": Any, "List": List, "Decimal": D, "ZERO": D("0.00"), "money": lambda v: D(v).quantize(D("0.01"), rounding=ROUND_HALF_UP)}
exec(textwrap.dedent(ast.get_source_segment(src, node)), ns); aggregate = ns["aggregate_by_category"]
class Acc:
    NORMAL_BALANCES = {"Revenue": "CR", "Expense": "DR"}
    def __init__(self, t): self.account_type = t
EXPENSES = ["RCDSO Dues", "CDA Membership", "Ontario Dental Association", "Other Professional Dues", "Malpractice Insurance",
            "Liability Insurance", "CE Courses", "Conferences & Seminars", "CE Travel & Accommodation", "Books & Materials (CE)",
            "Loupes & Personal Instruments", "Clinical Software & Subscriptions", "Work Laptop & Hardware", "Accounting & Tax Preparation",
            "Legal Fees", "Consulting Fees", "Vehicle Fuel", "Vehicle Maintenance", "Vehicle Parking & Tolls",
            "Vehicle Insurance (Business Portion)", "Cell Phone (Business Portion)", "Home Office Expenses", "Bank Charges & Fees"]
REVENUE = "Associate Fees"          # stand-in for the user's income account
gl = types.SimpleNamespace(accounts={REVENUE: Acc("Revenue"), **{a: Acc("Expense") for a in EXPENSES}})
def make_lines(revenue, per_expense):
    lines = [types.SimpleNamespace(account_name=REVENUE, dr=D(0), cr=D(revenue))]
    lines += [types.SimpleNamespace(account_name=a, dr=D(per_expense), cr=D(0)) for a in EXPENSES]
    return lines
cmap = {"self_employment": [REVENUE] + [{"account": a, "sign": -1} for a in EXPENSES]}

d = tempfile.mkdtemp(); os.makedirs(d + "/s"); os.makedirs(d + "/u")
for f in ("canada_federal.json", "canada_ontario.json"): shutil.copy("shipped/" + f, d + "/s/")
shutil.copy("user/canada_self_employment.json", d + "/u/")
ts = tp.TaxTemplateLoader(d + "/s", d + "/u", shipped_manifest={}).load_layers(["canada_federal", "canada_ontario"], ["canada_self_employment"], ledger_currency="CAD")
assert "self_employment" in ts.all_buckets
tp.validate_category_map(set(ts.all_buckets), cmap)           # the profile map is valid against the loaded buckets

def run(revenue, per_expense):
    gl.get_transactions = lambda start_date, end_date: [types.SimpleNamespace(lines=make_lines(revenue, per_expense))]
    agg = aggregate(gl, "2026-01-01", "2026-12-31", cmap)
    return agg, tc.TaxCalculator(ts, {"category_map": cmap, "user_inputs": {}, "skipped_inputs": []}, agg).compute()

agg, r = run(150000, 1000)                      # 150,000 revenue - 23 x 1,000 expenses = 127,000 net
assert agg["self_employment"] == D("127000.00"), agg
b = r["breakdown"]["buckets"]["self_employment"]; assert b["raw"] == b["taxable"] == D("127000.00")
print("net business income:", agg["self_employment"], "| total_income:", r["total_income"], "| income tax:", r["income_tax_owed"])
levies = r["breakdown"].get("levies") or r.get("levies")
print("levies:", json.dumps(levies, default=str))
agg, r = run(10000, 1000)                       # expenses exceed revenue -> net loss
assert agg["self_employment"] == D("-13000.00") and r["total_income"] == 0
assert any("clamped" in w for w in r["breakdown"]["warnings"])
print("SELF-EMPLOYMENT MODULE TESTS PASSED")
