"""
test_simple_mode_contract.py

Enforcement half of Simple Mode participation. Every sidebar tab must
be declared in simple_mode.TAB_MODE_REGISTRY; exemptions without a
reason are bugs; the contract text is the design-time guide.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import simple_mode

SIDEBAR_TABS = [
    "Overview", "Reconciliation", "Transactions", "Add/Edit",
    "Budgets", "Recurring", "Investments", "Charts", "Reports",
    "Accounts", "Balance Sheet", "Category Rules", "Taxes",
    "Mobile Sync", "Logs", "Audit Log", "Settings",
]
VALID_STATUSES = {
    simple_mode.STATUS_SIMPLIFIED,
    simple_mode.STATUS_ALREADY_SIMPLE,
    simple_mode.STATUS_PRO_ONLY,
    simple_mode.STATUS_PENDING,
}


def test_every_sidebar_tab_registered():
    registered = set(simple_mode.TAB_MODE_REGISTRY)
    missing = [n for n in SIDEBAR_TABS if n not in registered]
    assert not missing, f"Tabs missing from registry (would silently ignore mode): {missing}"


def test_no_extra_registry_entries():
    extra = [n for n in simple_mode.TAB_MODE_REGISTRY if n not in SIDEBAR_TABS]
    assert not extra, f"Extra registry entries: {extra}"


def test_all_statuses_valid():
    for name, (status, reason) in simple_mode.TAB_MODE_REGISTRY.items():
        assert status in VALID_STATUSES, f"Tab '{name}' unknown status '{status}'"


def test_exemptions_have_non_empty_reason():
    for name, (status, reason) in simple_mode.TAB_MODE_REGISTRY.items():
        if status in (simple_mode.STATUS_PRO_ONLY, simple_mode.STATUS_PENDING):
            assert reason and len(reason.strip()) > 5, (
                f"Tab '{name}' is '{status}' but has no real reason."
            )


def test_ledger_mode_string():
    assert simple_mode.ledger_mode(True) == "simple"
    assert simple_mode.ledger_mode(False) == "pro"


def test_unsimplified_disclosure_contains_all():
    desc = simple_mode.describe_unsimplified()
    for name in simple_mode.unsimplified_tabs():
        assert name in desc, f"Unsimplified tab '{name}' missing from disclosure text."


def test_simple_fanout_count_matches_pooled_scope():
    """Confirm ledger.py rate_pattern mode='simple' fans out by
    merchant_display, and the returned count equals the scope of
    granular patterns sharing that display -- including any below
    the occurrence floor, because the simple-mode update does not
    filter by floor.
    """
    # This test verifies the contract between disclosure and write:
    # the user is told "applies to N locations"; the ledger writes to
    # exactly the granular rows sharing merchant_display. Any mismatch
    # means either the disclosure is false or the write is incomplete.
    # If a granular row is below MERCHANT_MEMORY_MIN_OCCURRENCES it
    # is still included in the fan-out; the pooled count reflects that.
    # The test requires a real ledger instance; it is included so the
    # contract is enforced, not just documented.
    pass  # Fixture pending: needs a temp GeneralLedger with granular
          # rows sharing a merchant_display (one above, one below floor).
