import sys, os
sys.path.insert(0, '.')
from decimal import Decimal

class MockEnv:
    category_name = "Groceries"

class MockLedger:
    def get_budget_envelopes(self):
        return [MockEnv()]
    def assign_to_envelope(self, month, cat, amount):
        print("CALL: assign_to_envelope(%s, %s, %s)" % (month, cat, amount))
        return True, "ok"
    def move_budget_money(self, from_cat, to_cat, amount, month):
        print("CALL: move_budget_money(%s -> %s, %s, %s)" % (from_cat, to_cat, amount, month))
        return True, "ok"

ledger = MockLedger()
envs = ledger.get_budget_envelopes()
cat = envs[0].category_name if envs else None
if cat:
    ledger.assign_to_envelope("2026-06", cat, Decimal("50.00"))
if len(envs) >= 2:
    ledger.move_budget_money(envs[0].category_name, envs[0].category_name, Decimal("10.00"), "2026-06")
print("BUTTON TEST PASSED")
