import pytest
import tempfile
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ledger import GeneralLedger
from decimal import Decimal
from audit import AuditLogReader

@pytest.fixture
def temp_ledger():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_ledger.db")
        test_dek_hex = os.urandom(32).hex()
        gl = GeneralLedger(db_path, test_dek_hex)
        yield gl

# ==========================================
# 1. CATEGORY RULES & TAX MAPPINGS
# ==========================================
def test_category_rules_application(temp_ledger):
    """Test that keyword rules correctly map descriptions to expense accounts."""
    temp_ledger.add_category_rule("TIM HORTONS", "Groceries", case_sensitive=False, enabled=True)

    assert temp_ledger.apply_category_rules("TIM HORTONS #1234") == "Groceries"
    assert temp_ledger.apply_category_rules("tim hortons") == "Groceries" # Case insensitive
    assert temp_ledger.apply_category_rules("STARBUCKS") is None

# ==========================================
# 2. AUDIT TRAIL IMMUTABILITY
# ==========================================
def test_audit_trail_on_edit(temp_ledger):
    """Ensure editing a transaction logs the OLD state, not the new one."""
    ok, _ = temp_ledger.record_transaction(
        "2026-01-01", "Original", [("Checking Account", 0, 10), ("Groceries", 10, 0)]
    )
    tx_id = temp_ledger.transactions[-1].id

    # Edit the transaction to $15
    new_lines = [(None, "Checking Account", 0, 15), (None, "Groceries", 15, 0)]
    temp_ledger.edit_transaction(tx_id, "2026-01-01", "Updated", new_lines)

    history = temp_ledger.get_transaction_history(tx_id)
    assert len(history) == 2
    # Append-only edit: the original tx is marked "voided" (not "updated") --
    # its replacement is a brand new tx id with its own "created" entry.
    assert history[1]["change_type"] == "voided"

    # The history payload MUST contain the old $10 lines
    old_lines = history[1]["lines"]
    amounts = [Decimal(l["dr"]) + Decimal(l["cr"]) for l in old_lines]
    assert Decimal("10.00") in amounts
    assert Decimal("15.00") not in amounts

def test_audit_trail_on_delete(temp_ledger):
    """Ensure deleting a transaction leaves a 'deleted' tombstone in history."""
    ok, _ = temp_ledger.record_transaction(
        "2026-01-01", "To Delete", [("Checking Account", 0, 50), ("Groceries", 50, 0)]
    )
    tx_id = temp_ledger.transactions[-1].id

    temp_ledger.void_transaction(tx_id)

    history = temp_ledger.get_transaction_history(tx_id)
    assert len(history) == 2
    # Append-only delete: the original tx is marked "voided", and a separate
    # reversal tx (its own id) carries the offsetting "created" entry.
    assert history[1]["change_type"] == "voided"

# ==========================================
# 3. AUDIT LOG PAGINATION (FIXED)
# ==========================================
def test_audit_log_pagination():
    """Ensure the AuditLogReader correctly paginates results without overlap."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "pagination_test.db")
        passphrase = "PaginationTest123!"

        # Setup: Create a ledger and insert 25 transactions
        test_dek_hex = os.urandom(32).hex()
        gl = GeneralLedger(db_path, test_dek_hex)
        for i in range(25):
            gl.record_transaction(
                "2026-01-01", f"Tx {i}",
                [("Checking Account", 10, 0), ("Groceries", 0, 10)]
            )

        # Test the AuditLogReader
        reader = AuditLogReader(db_path, test_dek_hex)
        page1 = reader.get_audit_log(limit=10, offset=0)
        page2 = reader.get_audit_log(limit=10, offset=10)

        assert len(page1) == 10
        assert len(page2) == 10

        # Ensure no overlap between pages
        page1_ids = {entry["id"] for entry in page1}
        page2_ids = {entry["id"] for entry in page2}
        assert page1_ids.isdisjoint(page2_ids), "Pagination overlap detected!"
