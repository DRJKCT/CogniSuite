"""
test_forensic_integrity.py
For CogniFinance Quarterly Deep Audit.
Verifies that the append-only ledger and cryptographic hash chain
successfully detect raw SQL tampering and enforce immutable accounting.
"""
import pytest
import os
import tempfile
from sqlcipher3 import dbapi2 as sqlite3
from ledger import GeneralLedger

# Generate a dummy 256-bit DEK for testing the SQLCipher connection
DUMMY_DEK_HEX = os.urandom(32).hex()

@pytest.fixture
def forensic_ledger(tmp_path):
    """Creates a temporary, isolated ledger for forensic testing."""
    db_path = tmp_path / "forensic_test.db"

    # Create the ledger and post a few transactions
    gl = GeneralLedger(str(db_path), DUMMY_DEK_HEX)
    gl.create_account("Checking", "Asset")
    gl.create_account("Salary", "Revenue")
    gl.create_account("Groceries", "Expense")

    gl.record_transaction("2024-01-01", "Paycheck", [("Checking", 1000, 0), ("Salary", 0, 1000)])
    gl.record_transaction("2024-01-05", "Food", [("Groceries", 50, 0), ("Checking", 0, 50)])

    yield gl, str(db_path)

def test_append_only_voiding(forensic_ledger):
    """Verifies that voiding a transaction creates a reversal and doesn't delete data."""
    gl, db_path = forensic_ledger
    tx_id = gl.transactions[0].id

    # Void the transaction
    ok, msg = gl.void_transaction(tx_id)
    assert ok, f"Voiding failed: {msg}"

    # Verify the original is marked voided in the DB via raw SQL
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(f"PRAGMA key = \"x'{DUMMY_DEK_HEX}'\"")
    cur.execute("SELECT is_voided, reversed_by_tx_id FROM sql_transactions WHERE id = ?", (tx_id,))
    is_voided, reversed_by = cur.fetchone()
    conn.close()

    assert is_voided == 1, "Original transaction was not marked as voided."
    assert reversed_by is not None, "Original transaction lacks a reversal pointer."

def test_hash_chain_tamper_detection(forensic_ledger):
    """
    Verifies that altering a historical record via raw SQL breaks the
    cryptographic chain, simulating an attacker with direct DB file access.
    """
    gl, db_path = forensic_ledger

    # 1. Verify the chain is currently intact
    is_valid, msg = gl.verify_audit_chain_integrity()
    assert is_valid, f"Chain broken before tampering: {msg}"

    # 2. Simulate an attacker using raw SQL to alter a past transaction's amount
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(f"PRAGMA key = \"x'{DUMMY_DEK_HEX}'\"")

    # Find the first history entry and change its lines_json
    cur.execute("SELECT id, lines_json FROM sql_transaction_history ORDER BY id ASC LIMIT 1")
    hid, original_json = cur.fetchone()

    tampered_json = original_json.replace("1000", "9999") # Inject fake money
    cur.execute("UPDATE sql_transaction_history SET lines_json = ? WHERE id = ?", (tampered_json, hid))
    conn.commit()
    conn.close()

    # 3. Re-open the ledger and verify the chain catches the tampering
    gl2 = GeneralLedger(db_path, DUMMY_DEK_HEX)
    is_valid, msg = gl2.verify_audit_chain_integrity()

    assert not is_valid, "CRITICAL: Hash chain failed to detect SQL tampering!"
    assert f"history ID {hid}" in msg or "Tampering detected" in msg, f"Error message didn't pinpoint the tampered ID: {msg}"

def test_edit_transaction_creates_void_history(forensic_ledger):
    """
    Verifies that the append-only 'edit' method correctly voids the original
    transaction and logs the correct history states.
    """
    gl, db_path = forensic_ledger
    tx_id = gl.transactions[-1].id # The "Food" transaction

    # Edit the transaction
    ok, msg = gl.edit_transaction(
        tx_id, "2024-01-05", "Updated Food",
        [(None, "Groceries", 60, 0), (None, "Checking", 0, 60)]
    )
    assert ok, f"Edit failed: {msg}"

    # Check the history of the ORIGINAL transaction ID
    history = gl.get_transaction_history(tx_id)

    # It should have exactly 2 entries: "created" and "voided"
    assert len(history) == 2
    assert history[0]["change_type"] == "created"
    assert history[1]["change_type"] == "voided"
