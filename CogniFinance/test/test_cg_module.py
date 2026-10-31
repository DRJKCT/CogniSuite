import sys, json, types, logging
from decimal import Decimal as D
# the calculator imports ledger (ZERO) and tax_format; stub what we don't have
if "tax_format" not in sys.modules:
    try: import tax_format
    except ImportError:
        m = types.ModuleType("tax_format"); m.ROUNDING_MODES = ("half_up",); m.currency_minor_units = lambda c: 2
        from decimal import ROUND_HALF_UP
        m.round_to_increment = lambda v, inc=D("0.01"), mode="half_up": v.quantize(D("0.01"), rounding=ROUND_HALF_UP)
        sys.modules["tax_format"] = m
if "ledger" not in sys.modules:
    try: import ledger
    except Exception:
        m = types.ModuleType("ledger"); m.ZERO = D("0.00"); sys.modules["ledger"] = m
import tax_parser as tp
from tax_calculator import TaxCalculator

mod = json.load(open("user/canada_capital_gains.json", encoding="utf-8"))
tp.validate_schema(mod)                                    # real schema validation
fed = tp.upgrade_template(json.load(open("shipped/canada_federal.json")))
ont = json.load(open("shipped/canada_ontario.json"))
assert tp.module_accepted_by_bases([fed], mod), "federal base must accept the module"
assert tp.module_accepted_by_bases([fed, ont], mod)       # ANY-base rule
assert not tp.module_accepted_by_bases([ont], mod)         # Ontario alone doesn't declare it -> hidden
mod_bad_version = dict(mod, valid_from=2027); assert not tp.module_accepted_by_bases([fed], mod_bad_version)

loader = tp.TaxTemplateLoader("shipped", "user", shipped_manifest={})
listing = loader.list_available_templates()
assert "canada_capital_gains" in json.dumps(listing, default=str), listing.keys()
ts = loader.load_layers(["canada_federal", "canada_ontario"], ["canada_capital_gains"], ledger_currency="CAD")
assert "capital_gains" in ts.all_buckets and [m for m in ts.modules]
print("tier:", ts.tiers.get("canada_capital_gains"))

def compute(gains):
    profile = {"category_map": {}, "user_inputs": {}, "skipped_inputs": []}
    c = TaxCalculator(ts, profile, {"capital_gains": D(gains)})
    return c.compute(), c

r, c = compute("600.00")
b = r["breakdown"]["buckets"]["capital_gains"]
assert b["raw"] == D("600.00") and b["taxable"] == D("300.00"), b           # 50% inclusion
assert r["total_income"] == D("300.00")
r, c = compute("-300.00")
assert r["breakdown"]["buckets"]["capital_gains"]["taxable"] == D("0.00")
assert any("clamped" in w for w in r["breakdown"]["warnings"]), r["breakdown"]["warnings"]
assert c._negative_raw["capital_gains"] == D("300.00")
print("MODULE LOADS, VALIDATES, COMPUTES")
