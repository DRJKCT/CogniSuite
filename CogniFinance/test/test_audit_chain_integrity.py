"""
test_audit_chain_integrity.py
For CogniFinance ver 1.9
Runs exclusively during the QUARTERLY deep integrity check.
Verifies that the cryptographic hash chain of the audit trail has not been
tampered with via raw SQL or external database manipulation.
"""
import os
import pytest
from ledger import GeneralLedger

def test_audit_chain_integrity():
    """
    Opens the live database using credentials injected via QProcess
    environment variables and verifies the SHA-256 hash chain of
    sql_transaction_history.
    """
    db_path = os.environ.get("COGNIFINANCE_DB_PATH")
    passphrase = os.environ.get("COGNIFINANCE_DB_PASSPHRASE")

    if not db_path or not passphrase:
        pytest.skip("Live database credentials not provided in environment. (Expected during standard CI/CD or local dev runs).")

    if not os.path.exists(db_path):
        pytest.skip(f"Database file not found at {db_path}")

    # Open the live ledger
    gl = GeneralLedger(db_path, passphrase)

    # Run the verification
    is_valid, message = gl.verify_audit_chain_integrity()

    # If the chain is broken, pytest will fail the deep audit and
    # output the exact history ID where the tampering occurred.
    assert is_valid, f"Audit chain verification failed: {message}"
