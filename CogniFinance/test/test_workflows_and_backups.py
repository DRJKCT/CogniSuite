import os
import sys
import tempfile
import pytest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger import GeneralLedger

@pytest.fixture
def temp_ledger():
    """Creates a fresh, empty, encrypted SQLCipher database in a temporary directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_ledger.db")
        gl = GeneralLedger(db_path, "TestPassphrase123!")
        yield gl

# ==========================================
# 1. CREDIT CARD LIFE CYCLES
# ==========================================
def test_credit_card_lifecycle(temp_ledger):
    """
    Verify that the ledger correctly handles the accounting conventions
    for Liabilities (Credit Cards). Purchases increase the liability
    (Credit), and payments decrease the liability (Debit).
    """
    # Ensure accounts exist (they are seeded by default, but let's be explicit)
    if "Credit Card" not in temp_ledger.accounts:
        temp_ledger.create_account("Credit Card", "Liability")
    if "Groceries" not in temp_ledger.accounts:
        temp_ledger.create_account("Groceries", "Expense")

    # 1. Make a $100 purchase on the Credit Card
    # DR Groceries 100, CR Credit Card 100
    ok, _ = temp_ledger.record_transaction(
        "2026-01-01", "Grocery Store",
        [("Groceries", 100, 0), ("Credit Card", 0, 100)]
    )
    assert ok is True

    # Groceries (Expense, normal DR) should be 100
    assert temp_ledger.accounts["Groceries"].get_balance() == Decimal("100.00")
    # Credit Card (Liability, normal CR) should be 100
    assert temp_ledger.accounts["Credit Card"].get_balance() == Decimal("100.00")

    # 2. Make a $40 payment from Checking to Credit Card
    # DR Credit Card 40, CR Checking Account 40
    ok, _ = temp_ledger.record_transaction(
        "2026-01-05", "CC Payment",
        [("Credit Card", 40, 0), ("Checking Account", 0, 40)]
    )
    assert ok is True

    # Credit Card balance should now be 60 (100 CR - 40 DR = 60 CR)
    assert temp_ledger.accounts["Credit Card"].get_balance() == Decimal("60.00")
    # Checking Account (Asset, normal DR) should be -40 (since we didn't seed it with income)
    assert temp_ledger.accounts["Checking Account"].get_balance() == Decimal("-40.00")

# ==========================================
# 2. TRANSFERS (NET WORTH PRESERVATION)
# ==========================================
def test_transfers_do_not_affect_net_worth(temp_ledger):
    """
    Ensure that moving money between Asset and Liability accounts
    strictly preserves the overall Net Worth (Assets - Liabilities)
    and does not accidentally trigger Income or Expense accounts.
    """
    # Seed the checking account with $1000
    temp_ledger.record_transaction(
        "2026-01-01", "Opening Balance",
        [("Checking Account", 1000, 0), ("Uncategorized Income", 0, 1000)]
    )

    # Create a Savings account
    temp_ledger.create_account("Savings", "Asset")

    initial_net_worth = (
        temp_ledger.balance_sheet()["assets"]["total"] -
        temp_ledger.balance_sheet()["liabilities"]["total"]
    )

    # Transfer $200 from Checking to Savings
    ok, _ = temp_ledger.transfer_funds("2026-01-02", "Move to Savings", "Checking Account", "Savings", 200)
    assert ok is True

    assert temp_ledger.accounts["Checking Account"].get_balance() == Decimal("800.00")
    assert temp_ledger.accounts["Savings"].get_balance() == Decimal("200.00")

    # Net worth must remain exactly the same
    final_net_worth = (
        temp_ledger.balance_sheet()["assets"]["total"] -
        temp_ledger.balance_sheet()["liabilities"]["total"]
    )
    assert final_net_worth == initial_net_worth

    # Test Liability Transfer (Paying off a credit card)
    temp_ledger.record_transaction(
        "2026-01-03", "CC Purchase",
        [("Groceries", 150, 0), ("Credit Card", 0, 150)]
    )

    nw_before_payoff = (
        temp_ledger.balance_sheet()["assets"]["total"] -
        temp_ledger.balance_sheet()["liabilities"]["total"]
    )

    # Pay off the $150 credit card balance
    temp_ledger.transfer_funds("2026-01-04", "CC Payoff", "Checking Account", "Credit Card", 150)

    nw_after_payoff = (
        temp_ledger.balance_sheet()["assets"]["total"] -
        temp_ledger.balance_sheet()["liabilities"]["total"]
    )

    # Paying debt reduces assets (cash) and reduces liabilities (debt) by the exact same amount.
    # Therefore, Net Worth remains unchanged.
    assert nw_before_payoff == nw_after_payoff
    assert temp_ledger.accounts["Credit Card"].get_balance() == Decimal("0.00")

# ==========================================
# 3. BACKUP & RESTORE (SQLCIPHER EXPORT)
# ==========================================
def test_encrypted_backup_and_restore(temp_ledger):
    """
    Verify that the native SQLCipher backup mechanism creates a
    fully valid, encrypted clone of the database, and that all
    transactions, accounts, and audit history survive the restore.
    """
    # 1. Populate the ledger
    temp_ledger.record_transaction(
        "2026-01-01", "Income",
        [("Checking Account", 1000, 0), ("Salary Income", 0, 1000)]
    )
    temp_ledger.record_transaction(
        "2026-01-02", "Groceries",
        [("Groceries", 50, 0), ("Checking Account", 0, 50)]
    )

    original_tx_count = len(temp_ledger.transactions)
    original_checking_bal = temp_ledger.accounts["Checking Account"].get_balance()

    with tempfile.TemporaryDirectory() as tmpdir:
        backup_path = os.path.join(tmpdir, "backup.db")

        # 2. Create Backup
        ok, msg = temp_ledger.create_encrypted_backup(backup_path)
        assert ok is True
        assert os.path.exists(backup_path)

        # 3. Restore (Open the backup as a brand new GeneralLedger instance)
        # It MUST require the original passphrase to unlock
        restored_ledger = GeneralLedger(backup_path, "TestPassphrase123!")

        # 4. Verify data integrity
        assert len(restored_ledger.transactions) == original_tx_count
        assert restored_ledger.accounts["Checking Account"].get_balance() == original_checking_bal

        # 5. Verify Audit History survives the backup
        # Edit a transaction in the original ledger
        tx_id = temp_ledger.transactions[-1].id
        temp_ledger.update_transaction(
            tx_id, "2026-01-02", "Updated Groceries",
            [("Groceries", 60, 0), ("Checking Account", 0, 60)]
        )

        # Backup again
        backup_path_2 = os.path.join(tmpdir, "backup2.db")
        temp_ledger.create_encrypted_backup(backup_path_2)

        # Restore and check history
        restored_ledger_2 = GeneralLedger(backup_path_2, "TestPassphrase123!")
        history = restored_ledger_2.get_transaction_history(tx_id)

        assert len(history) == 2
        assert history[0]["change_type"] == "created"
        assert history[1]["change_type"] == "updated"
