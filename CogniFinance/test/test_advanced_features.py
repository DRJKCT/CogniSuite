import os
import sys
import tempfile
import pytest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger import GeneralLedger
from passphrase_recovery import (
    write_recovery_escrow,
    read_recovery_escrow,
    escrow_exists,
    delete_recovery_escrow,
    generate_recovery_passphrase
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ==========================================
# 1. REKEY DATA STABILITY
# ==========================================
def test_rekey_data_stability():
    """
    Ensure that changing the database passphrase (rekeying) doesn't
    corrupt data, and that the old passphrase is immediately invalidated.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_rekey.db")
        gl = GeneralLedger(db_path, "OldPassphrase123!")

        # Post some data
        gl.record_transaction("2026-01-01", "Paycheck", [
            ("Checking Account", 2500.00, 0),
            ("Salary Income", 0, 2500.00)
        ])
        original_balance = gl.accounts["Checking Account"].get_balance()

        # Change the passphrase
        ok, msg = gl.rekey("NewPassphrase456!")
        assert ok is True

        # 1. Verify we can open the DB with the NEW passphrase and data is intact
        gl_new = GeneralLedger(db_path, "NewPassphrase456!")
        assert len(gl_new.transactions) == 1
        assert gl_new.accounts["Checking Account"].get_balance() == original_balance

        # 2. Verify the OLD passphrase is now completely locked out
        with pytest.raises(ValueError):
            GeneralLedger(db_path, "OldPassphrase123!")

# ==========================================
# 2. ESCROW RECOVERY FLOW
# ==========================================
def test_escrow_recovery_flow():
    """
    Test the full lifecycle of the passphrase escrow file:
    creation, successful decryption, failed decryption, and deletion.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "dummy.db")
        open(db_path, 'a').close() # Touch file so path resolution works

        primary_pass = "CorrectHorseBatteryStaple"
        recovery_pass = generate_recovery_passphrase()

        # 1. Write Escrow
        write_recovery_escrow(db_path, primary_pass, recovery_pass)
        assert escrow_exists(db_path)

        # 2. Read with CORRECT recovery key
        recovered = read_recovery_escrow(db_path, recovery_pass)
        assert recovered == primary_pass

        # 3. Read with WRONG recovery key (Must return None, not crash)
        wrong_key = generate_recovery_passphrase()
        assert read_recovery_escrow(db_path, wrong_key) is None

        # 4. Delete Escrow
        delete_recovery_escrow(db_path)
        assert not escrow_exists(db_path)

# ==========================================
# 3. "BALANCE AS OF" & RECONCILIATION
# ==========================================
def test_balance_as_of_and_reconciliation():
    """
    Verify that the ledger correctly calculates running balances up to
    a specific date, and accurately flags discrepancies against imported
    bank statement balances.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_recon.db")
        gl = GeneralLedger(db_path, "TestPass!")

        # Post transactions across different dates
        gl.record_transaction("2026-01-01", "Deposit", [("Checking Account", 1000, 0), ("Salary Income", 0, 1000)])
        gl.record_transaction("2026-01-05", "Groceries", [("Groceries", 100, 0), ("Checking Account", 0, 100)])
        gl.record_transaction("2026-01-10", "Bonus", [("Checking Account", 500, 0), ("Salary Income", 0, 500)])

        # 1. Test running balances at different points in time
        assert gl.running_balance_as_of("Checking Account", "2026-01-01") == Decimal("1000.00")
        assert gl.running_balance_as_of("Checking Account", "2026-01-05") == Decimal("900.00")
        assert gl.running_balance_as_of("Checking Account", "2026-01-10") == Decimal("1400.00")

        # 2. Test Reconciliation - Exact Match
        gl.record_statement_balance("Checking Account", "2026-01-05", 900.00, source="Bank PDF")

        # 3. Test Reconciliation - Mismatch (Bank says 1350, Ledger says 1400)
        gl.record_statement_balance("Checking Account", "2026-01-10", 1350.00, source="Bank PDF")

        report = gl.reconciliation_report("Checking Account")
        assert len(report) == 2

        match_row = next(r for r in report if r["date"] == "2026-01-05")
        assert match_row["matched"] is True
        assert match_row["difference"] == Decimal("0.00")

        mismatch_row = next(r for r in report if r["date"] == "2026-01-10")
        assert mismatch_row["matched"] is False
        assert mismatch_row["difference"] == Decimal("50.00") # 1400 - 1350

# ==========================================
# 4. IMPORT/EXPORT IDEMPOTENCY
# ==========================================
def test_csv_import_export_idempotency():
    """
    Ensure that exporting the ledger to CSV and importing it into a
    fresh database results in the exact same mathematical state, and
    that exporting it a second time produces an identical file.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path_1 = os.path.join(tmpdir, "ledger1.db")
        gl1 = GeneralLedger(db_path_1, "Pass1")

        # Post diverse transactions
        gl1.record_transaction("2026-01-01", "Income", [("Checking Account", 1000, 0), ("Salary Income", 0, 1000)])
        gl1.record_transaction("2026-01-02", "Groceries", [("Groceries", 150.55, 0), ("Checking Account", 0, 150.55)])

        original_balance = gl1.accounts["Checking Account"].get_balance()
        csv_path = os.path.join(tmpdir, "export.csv")

        # 1. Export
        gl1.export_journal_csv(csv_path)
        assert os.path.exists(csv_path)

        # 2. Import into a brand new, empty ledger
        db_path_2 = os.path.join(tmpdir, "ledger2.db")
        gl2 = GeneralLedger(db_path_2, "Pass2")
        imported_count = gl2.import_ledger_csv(csv_path)

        # 3. Assert state matches perfectly
        assert imported_count == 2
        assert len(gl2.transactions) == len(gl1.transactions)
        assert gl2.accounts["Checking Account"].get_balance() == original_balance

        # 4. Export again to verify deterministic output (byte-for-byte identical)
        csv_path_2 = os.path.join(tmpdir, "export2.csv")
        gl2.export_journal_csv(csv_path_2)

        with open(csv_path, 'r', encoding='utf-8') as f1, open(csv_path_2, 'r', encoding='utf-8') as f2:
            assert f1.read() == f2.read()
