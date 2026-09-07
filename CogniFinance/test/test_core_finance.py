import os
import sys
# Add the parent directory to the path so we can import ledger.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tempfile
import pytest
from decimal import Decimal
from datetime import datetime

# Import your core modules
from ledger import GeneralLedger, money, ZERO
from pdf_import import normalize_statement_date

# ==========================================
# FIXTURES
# ==========================================
@pytest.fixture
def temp_ledger():
    """
    Creates a fresh, empty, encrypted SQLCipher database in a temporary
    directory for each test. Automatically cleans up after the test runs.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_ledger.db")
        # Initialize with a standard test passphrase
        test_dek_hex = os.urandom(32).hex()
        gl = GeneralLedger(db_path, test_dek_hex)

        # Explicitly create default accounts to ensure tests are isolated
        # and don't rely on hidden auto-creation logic.
        gl.create_account("Checking Account", "Asset")
        gl.create_account("Groceries", "Expense")
        gl.create_account("Salary Income", "Revenue")

        yield gl

# ==========================================
# 1. MATHEMATICAL & ACCOUNTING INVARIANTS
# ==========================================
def test_decimal_rounding_half_up():
    """Ensure we are strictly using ROUND_HALF_UP and not Python's default banker's rounding."""
    assert money("10.005") == Decimal("10.01")
    assert money("10.004") == Decimal("10.00")
    assert money("-10.005") == Decimal("-10.01")

def test_unbalanced_transaction_rejected(temp_ledger):
    """The ledger must physically reject transactions where Debits != Credits."""
    ok, msg = temp_ledger.record_transaction(
        "2026-01-01",
        "Unbalanced Test",
        [("Checking Account", 10.00, 0), ("Groceries", 0, 9.99)] # Off by 1 cent
    )
    assert ok is False
    assert "out of balance" in msg.lower()

def test_accounting_equation_holds(temp_ledger):
    """Assets = Liabilities + Equity + (Revenue - Expenses)"""
    # Post some seed data
    ok1, msg1 = temp_ledger.record_transaction("2026-01-01", "Salary", [("Checking Account", 1000, 0), ("Salary Income", 0, 1000)])
    assert ok1 is True, f"Failed to post salary: {msg1}"

    ok2, msg2 = temp_ledger.record_transaction("2026-01-02", "Groceries", [("Groceries", 100, 0), ("Checking Account", 0, 100)])
    assert ok2 is True, f"Failed to post groceries: {msg2}"

    bs = temp_ledger.balance_sheet()

    assets = bs["assets"]["total"]
    liab = bs["liabilities"]["total"]
    eq = bs["equity"]["total"]
    rev = bs["income"]["total"]
    exp = bs["expenses"]["total"]

    # The fundamental accounting equation
    assert assets == (liab + eq + rev - exp), f"Equation failed: {assets} != {liab} + {eq} + {rev} - {exp}"


# ==========================================
# 2. TEMPORAL & BOUNDARY EDGE CASES
# ==========================================
def test_date_inference_year_end_boundary():
    """
    Test the logic that prevents December transactions from being
    mislabeled as the new year if imported in early January.
    """
    # Reference date: Jan 5, 2026
    ref_date = datetime(2026, 1, 5)

    # "Dec 28" should resolve to 2025, not 2026
    assert normalize_statement_date("Dec 28", ref_date) == "2025-12-28"

    # "Jan 02" should resolve to 2026
    assert normalize_statement_date("Jan 02", ref_date) == "2026-01-02"

# ==========================================
# 3. IMPORT IDEMPOTENCY & DEDUPLICATION
# ==========================================
def test_duplicate_imported_id_rejected(temp_ledger):
    """Ensure the ledger prevents double-importing the exact same statement line."""
    lines = [("Checking Account", 0, 5.00), ("Groceries", 5.00, 0)]

    # First import should succeed
    ok1, msg1 = temp_ledger.record_transaction(
        "2026-01-01", "Coffee", lines, imported_id="stmt_123::0"
    )
    assert ok1 is True, f"First import should have succeeded, but failed with: {msg1}"

    # Second import with the same ID should fail gracefully
    ok2, msg2 = temp_ledger.record_transaction(
        "2026-01-01", "Coffee", lines, imported_id="stmt_123::0"
    )
    assert ok2 is False, "Second import with same ID should have been rejected"
    assert "duplicate" in msg2.lower(), f"Expected 'duplicate' in error message, got: {msg2}"

# ==========================================
# 4. SECURITY & INJECTION (THE "O'CONNOR" TEST)
# ==========================================
def test_passphrase_with_single_quote():
    """
    Ensure that a passphrase containing a single quote doesn't break
    the SQLCipher PRAGMA string formatting (SQL Injection prevention).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "quote_test.db")
        # This should successfully create and unlock the DB without throwing a sqlite3.ProgrammingError
        gl = GeneralLedger(db_path, "O'Connor's Budget!")
        assert gl is not None

        # Verify we can still post a transaction to prove the DB is fully functional
        ok, msg = gl.record_transaction(
            "2026-01-01", "Test",
            [("Checking Account", 10, 0), ("Salary Income", 0, 10)]
        )
        assert ok is True, f"Transaction failed with single-quote passphrase. Error: {msg}"

# ==========================================
# 5. AUDIT & STATE MUTATION (THE PAPER TRAIL)
# ==========================================
def test_edit_preserves_old_state_in_history(temp_ledger):
    """
    When a transaction is edited, the history table must record the state
    of the transaction *before* the edit, not after.
    """
    # 1. Post original transaction ($10.00)
    ok1, msg1 = temp_ledger.record_transaction(
        "2026-01-01", "Original Dinner",
        [("Checking Account", 0, 10.00), ("Groceries", 10.00, 0)],
        persist=True
    )
    assert ok1 is True, f"Initial post failed: {msg1}"
    tx_id = temp_ledger.transactions[-1].id

    # 2. Edit the transaction to $15.00
    new_lines = [(None, "Checking Account", 0, 15.00), (None, "Groceries", 15.00, 0)]
    ok2, msg2 = temp_ledger.edit_transaction(tx_id, "2026-01-01", "Updated Dinner", new_lines)
    assert ok2 is True, f"Update failed: {msg2}"

    # 3. Inspect the Audit Log
    history = temp_ledger.get_transaction_history(tx_id)
    assert len(history) == 2, f"Expected 2 history entries, got {len(history)}" # 1 for 'created', 1 for 'updated'

    updated_record = history[1]
    # Append-only edit: the original tx is marked "voided", not "updated" --
    # the new values live in a separate tx id's own "created" entry.
    assert updated_record["change_type"] == "voided"

    # The history payload should contain the OLD lines ($10.00), not the new ones ($15.00)
    old_lines = updated_record["lines"]
    amounts = [Decimal(l["dr"]) + Decimal(l["cr"]) for l in old_lines]
    assert Decimal("10.00") in amounts, f"Expected old amount 10.00 in history, got: {amounts}"
    assert Decimal("15.00") not in amounts, f"New amount 15.00 should not be in old history, got: {amounts}"
