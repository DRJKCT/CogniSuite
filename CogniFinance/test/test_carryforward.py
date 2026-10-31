import sys, json, types, copy, shutil, os, tempfile, importlib.util
from decimal import Decimal as D, ROUND_HALF_UP
m = types.ModuleType("tax_format"); m.ROUNDING_MODES = ("half_up",); m.currency_minor_units = lambda c: 2
m.round_to_increment = lambda v, inc=D("0.01"), mode="half_up": D(v).quantize(D("0.01"), rounding=ROUND_HALF_UP)
sys.modules["tax_format"] = m
l = types.ModuleType("ledger"); l.ZERO = D("0.00"); sys.modules["ledger"] = l

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path); mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod; spec.loader.exec_module(mod); return mod
tp = load("tax_parser.py", "tax_parser"); tc = load("tax_calculator.py", "tax_calculator")
tp_old = load("tax_parser.orig.py", "tax_parser_old"); tc_old = load("tax_calculator.orig.py", "tax_calculator_old")

def mkloader(parser, fed=None, mod=None):
    d = tempfile.mkdtemp(); os.makedirs(d + "/s"); os.makedirs(d + "/u")
    shutil.copy("shipped/canada_ontario.json", d + "/s/")
    json.dump(fed or json.load(open("shipped/canada_federal.json")), open(d + "/s/canada_federal.json", "w"))
    json.dump(mod or json.load(open("user/canada_capital_gains.json", encoding="utf-8")), open(d + "/u/canada_capital_gains.json", "w"))
    return parser.TaxTemplateLoader(d + "/s", d + "/u", shipped_manifest={})

def loadset(parser, **kw):
    return mkloader(parser, **kw).load_layers(["canada_federal", "canada_ontario"], ["canada_capital_gains"], ledger_currency="CAD")

ts = loadset(tp)
def run(gains, carried=None, skipped=(), bare=False, tset=None, calc=tc):
    ui = {}
    if carried is not None: ui["net_capital_loss"] = D(carried) if bare else {"value": str(carried)}
    c = calc.TaxCalculator(tset or ts, {"category_map": {}, "user_inputs": ui, "skipped_inputs": list(skipped)}, {"capital_gains": D(gains)})
    r = c.compute(); return r, r["breakdown"]["buckets"]["capital_gains"], r["carryforwards"]

# --- 1. loss year: nothing taxable, loss carried out
r, b, cf = run("-300"); assert b["taxable"] == 0 and cf == {"net_capital_loss": D("300.00")}, (b, cf)
# --- 2. carried loss fully absorbed by a bigger gain
r, b, cf = run("500", carried="300")
assert b["raw"] == D("500.00") and b["carryforward_applied"] == D("300.00") and b["taxable"] == D("100.00") and cf["net_capital_loss"] == 0, (b, cf)
assert any("Applied 300.00" in w for w in r["breakdown"]["warnings"])
# --- 3. gain smaller than the carried loss: capped, remainder carries on
r, b, cf = run("100", carried="300"); assert b["taxable"] == 0 and b["carryforward_applied"] == D("100.00") and cf["net_capital_loss"] == D("200.00"), (b, cf)
# --- 4. another loss year on top of carried: both roll on
r, b, cf = run("-50", carried="300"); assert b["taxable"] == 0 and cf["net_capital_loss"] == D("350.00"), cf
# --- 5. no carry-in: behaves as before; bare-value shape accepted; skipped/negative treated as zero
r, b, cf = run("600"); assert b["taxable"] == D("300.00") and cf["net_capital_loss"] == 0
r, b, cf = run("500", carried=300, bare=True); assert b["taxable"] == D("100.00")
r, b, cf = run("500", carried="300", skipped=["net_capital_loss"]); assert b["taxable"] == D("250.00") and cf["net_capital_loss"] == 0
r, b, cf = run("500", carried="-40"); assert b["taxable"] == D("250.00")
# --- 6. an income tax difference is real: carried loss lowers total income / tax
r_with, _, _ = run("500", carried="300"); r_without, _, _ = run("500")
assert r_with["total_income"] < r_without["total_income"] and r_with["income_tax_owed"] <= r_without["income_tax_owed"]

# --- 7. validation
def expect_error(fn, needle):
    try: fn()
    except tp.TemplateValidationError as e: assert needle in str(e), (needle, str(e)); return
    raise AssertionError("expected TemplateValidationError: " + needle)
fed_plain = json.load(open("shipped/canada_federal.json")); fed_plain.pop("carryforwards")
expect_error(lambda: loadset(tp, fed=fed_plain), "no loaded base declares")                       # base not updated
fed_wrong = copy.deepcopy(fed_plain); fed_wrong["carryforwards"] = [{"name": "net_capital_loss", "source": "negative_bucket", "bucket": "other"}]
expect_error(lambda: loadset(tp, fed=fed_wrong), "no loaded base declares")                       # wrong bucket
fed_src = copy.deepcopy(fed_plain); fed_src["carryforwards"] = [{"name": "net_capital_loss", "source": "unused_credits"}]
expect_error(lambda: loadset(tp, fed=fed_src), "no loaded base declares")                         # wrong source
bad = json.load(open("user/canada_capital_gains.json", encoding="utf-8"))
bad["buckets"]["capital_gains"]["carryforward_in"] = ""; expect_error(lambda: tp.validate_schema(bad), "must be the name")
bad["buckets"]["capital_gains"].update(primitive="separate_rate", carryforward_in="x", schedules={"national": {"rate": 0.1}})
expect_error(lambda: tp.validate_schema(bad), "only supported on inclusion_rate")

# --- 8. regression: a module WITHOUT carryforward_in computes identically under the ORIGINAL parser+calculator
plain_mod = json.load(open("user/canada_capital_gains.json", encoding="utf-8")); plain_mod["buckets"]["capital_gains"].pop("carryforward_in")
fed_old = json.load(open("shipped/canada_federal.json"))                       # carryforwards still declared: harmless w/o consumer
ts_new = loadset(tp, fed=fed_old, mod=plain_mod); ts_old = loadset(tp_old, fed=fed_old, mod=plain_mod)
for g in ("600", "-300", "0", "25000"):
    a, _, _ = run(g, tset=ts_new, calc=tc); b_, _, _ = run(g, tset=ts_old, calc=tc_old)
    assert json.dumps(a, default=str, sort_keys=True) == json.dumps(b_, default=str, sort_keys=True), g
print("CARRY-FORWARD TESTS PASSED")
