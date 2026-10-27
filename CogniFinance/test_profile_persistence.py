import sys, types, sqlite3, tempfile, json, ast, textwrap
from decimal import Decimal as D, ROUND_HALF_UP
from typing import Dict, Any, List
sys.path.insert(0, "/home/claude/t")                        # real tax_parser (+ stubs for what wasn't uploaded)
m = types.ModuleType("tax_format"); m.ROUNDING_MODES = ("half_up",); m.currency_minor_units = lambda c: 2
m.round_to_increment = lambda v, inc=D("0.01"), mode="half_up": D(v); sys.modules["tax_format"] = m
l = types.ModuleType("ledger"); l.ZERO = D("0.00"); l.GeneralLedger = object; sys.modules["ledger"] = l

# ---- PyQt6 stand-ins so the dialog module imports headlessly
class _S:
    def __init__(self, *a, **k): pass
for name, attrs in {
    "PyQt6": {}, "PyQt6.QtCore": {"Qt": _S, "QLocale": _S}, "PyQt6.QtGui": {"QFont": _S},
    "PyQt6.QtWidgets": {n: _S for n in ("QDialog","QVBoxLayout","QHBoxLayout","QLabel","QPushButton","QTreeWidget","QTreeWidgetItem",
                                          "QComboBox","QDialogButtonBox","QMessageBox","QGroupBox","QListWidget","QCheckBox")}}.items():
    mod = types.ModuleType(name); mod.__dict__.update(attrs); sys.modules[name] = mod

import tax_profile as tpf, tax_profile_dialog as dlg

class FakeGL:                                               # a real sqlite file, as the ledger's _connect() would give
    def __init__(self): self.path = tempfile.mktemp(suffix=".db")
    def _connect(self): c = sqlite3.connect(self.path); return c, c.cursor()
gl = FakeGL(); pm = tpf.TaxProfileManager(gl)
SEC = "CA/Default"

# ---- 1. the category map (with signed entries) really persists and round-trips
cmap = {"self_employment": ["Associate Fees", {"account": "RCDSO Dues", "sign": -1}],
        "capital_gains": ["Realized Gains — Investments", {"account": "Realized Losses — Investments", "sign": -1}]}
assert pm.save_section(SEC, cmap, {}, [])
pm2 = tpf.TaxProfileManager(gl)                              # a fresh manager = a new app session
assert pm2.load_section(SEC)["category_map"] == cmap

# ---- 2. dialog helpers: add many at once, subtract flag, no duplicates, toggle, remove
cm = {}
assert dlg.add_accounts_to_map(cm, "self_employment", ["Associate Fees"], False) == 1
assert dlg.add_accounts_to_map(cm, "self_employment", ["RCDSO Dues", "Vehicle Fuel", "Associate Fees"], True) == 2   # dup skipped
assert cm["self_employment"] == ["Associate Fees", {"account": "RCDSO Dues", "sign": -1}, {"account": "Vehicle Fuel", "sign": -1}]
assert dlg.toggle_sign_in_map(cm, "self_employment", "RCDSO Dues") and cm["self_employment"][1] == "RCDSO Dues"
assert dlg.toggle_sign_in_map(cm, "self_employment", "Associate Fees") and cm["self_employment"][0] == {"account": "Associate Fees", "sign": -1}
assert dlg.remove_account_from_map(cm, "self_employment", "Vehicle Fuel") and not dlg.remove_account_from_map(cm, "self_employment", "Nope")
assert [dlg.entry_account(e) for e in cm["self_employment"]] == ["Associate Fees", "RCDSO Dues"]
assert dlg.entry_sign("x") == 1 and dlg.entry_sign({"account": "x", "sign": -1}) == -1

# ---- 3. a map built by the dialog helpers is exactly what the aggregator needs to net correctly
src = open("/home/claude/l/ledger.py", encoding="utf-8").read()
node = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "aggregate_by_category")
ns = {"Dict": Dict, "Any": Any, "List": List, "Decimal": D, "ZERO": D("0.00"), "money": lambda v: D(v).quantize(D("0.01"), rounding=ROUND_HALF_UP)}
exec(textwrap.dedent(ast.get_source_segment(src, node)), ns)
class Acc:
    NORMAL_BALANCES = {"Revenue": "CR", "Expense": "DR"}
    def __init__(self, t): self.account_type = t
built = {}
dlg.add_accounts_to_map(built, "self_employment", ["Associate Fees"], False)
dlg.add_accounts_to_map(built, "self_employment", ["RCDSO Dues", "Vehicle Fuel"], True)
pm.save_section(SEC, built, {}, [])
saved = tpf.TaxProfileManager(gl).load_section(SEC)["category_map"]
fake = types.SimpleNamespace(accounts={"Associate Fees": Acc("Revenue"), "RCDSO Dues": Acc("Expense"), "Vehicle Fuel": Acc("Expense")})
L = lambda a, dr, cr: types.SimpleNamespace(account_name=a, dr=D(dr), cr=D(cr))
fake.get_transactions = lambda start_date, end_date: [types.SimpleNamespace(lines=[L("Associate Fees", 0, 10000), L("RCDSO Dues", 700, 0), L("Vehicle Fuel", 300, 0)])]
assert ns["aggregate_by_category"](fake, "2026-01-01", "2026-12-31", saved)["self_employment"] == D("9000.00")

# ---- 4. tax-tab selection persists, per country, separate from the category maps
assert pm.load_ui_state() == {}
st = tpf.record_selection({}, "CA", {"national": "canada_federal", "regional": "canada_ontario", "local": ""},
                          {"canada_capital_gains": True, "canada_self_employment": False})
assert pm.save_ui_state(st)
st2 = tpf.TaxProfileManager(gl).load_ui_state()                       # next session
assert st2["country"] == "CA" and tpf.stored_layer(st2, "CA", "regional") == "canada_ontario"
assert tpf.stored_layer(st2, "CA", "local") == "" and tpf.stored_layer(st2, "CA", "national") == "canada_federal"
assert tpf.stored_layer(st2, "US", "national") is None                # nothing stored for another country
assert tpf.stored_module_enabled(st2, "CA", "canada_capital_gains") and not tpf.stored_module_enabled(st2, "CA", "canada_self_employment")
assert tpf.stored_module_enabled(st2, "CA", "brand_new_module")        # unseen modules start on
# a module hidden by the current layers keeps its stored switch; another country is independent
st3 = tpf.record_selection(st2, "CA", {"national": "canada_federal", "regional": "", "local": ""}, {"canada_capital_gains": False})
assert not tpf.stored_module_enabled(st3, "CA", "canada_capital_gains") and not tpf.stored_module_enabled(st3, "CA", "canada_self_employment")
st4 = tpf.record_selection(st3, "US", {"national": "us_federal"}, {})
assert st4["country"] == "US" and tpf.stored_layer(st4, "CA", "national") == "canada_federal"

# ---- 5. saving/editing profile sections never disturbs the saved selection, and vice versa
pm.save_section(SEC, {}, {}, [])
pm.set_variant(SEC, "single"); pm.save_carryforwards(SEC, 2026, {"net_capital_loss": D("300")})
assert tpf.TaxProfileManager(gl).load_ui_state()["country"] == "CA"
pm.save_ui_state(st4)
data = pm.load_section(SEC); assert data["variant"] == "single" and data["carryforwards"]["2026"]["net_capital_loss"] == "300"
assert pm.carryforward_inputs_for(SEC, 2027)["net_capital_loss"]["value"] == "300"
print("PROFILE PERSISTENCE TESTS PASSED")
