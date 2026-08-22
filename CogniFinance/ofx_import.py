# ofx_import.py
import os
import logging
from decimal import Decimal
from datetime import datetime
from typing import List, Tuple, Dict, Any
from ofxparse import OfxParser

logger = logging.getLogger(__name__)

# Mapping OFX transaction types to standard Debit/Credit logic
# Note: OFX amounts are usually signed based on the account type.
# Asset (Checking): + is Deposit, - is Withdrawal
# Liability (Credit Card): - is Purchase (increases debt), + is Payment (decreases debt)

def _parse_ofx_amount(account_type: str, amount: Decimal, tran_type: str) -> Tuple[Decimal, Decimal]:
    """
    Converts a single-sided OFX amount into a balanced (Dr, Cr) tuple
    relative to the bank account.
    Returns (bank_dr, bank_cr). The uncategorized side is handled by the caller.
    """
    amount = abs(amount) # We will use the sign/type to determine direction

    is_asset = account_type in ("Bank", "Asset")
    is_liability = account_type in ("CreditLine", "Liability")

    # Normalize transaction type strings from various banks
    tran_type = (tran_type or "").upper()

    # Credits / Money In
    if tran_type in ("CREDIT", "DEP", "XFER", "INT", "DIV") or (is_asset and amount > 0):
        if is_asset:
            return (amount, Decimal("0.00")) # Dr Bank, Cr Income
        else:
            return (Decimal("0.00"), amount) # Cr Liability (Payment), Dr Income

    # Debits / Money Out
    else: # DEBIT, CHECK, POS, PAY, etc.
        if is_asset:
            return (Decimal("0.00"), amount) # Cr Bank, Dr Expense
        else:
            return (amount, Decimal("0.00")) # Dr Liability (Purchase), Cr Expense

def parse_ofx_to_ledger_transactions(file_path: str, ledger) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Parses an OFX/QFX file and converts it into a list of transaction dicts
    ready for ledger.record_transaction().

    Returns:
        (transactions_to_import, missing_accounts_to_create)
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"OFX file not found: {file_path}")

    try:
        with open(file_path, 'rb') as f:
            ofx = OfxParser.parse(f)
    except Exception as e:
        raise ValueError(f"Failed to parse OFX file. Is it a valid OFX/QFX file? Error: {e}")

    account = ofx.account
    if not account:
        raise ValueError("No account data found in the OFX file.")

    # 1. Determine the target Ledger Account Name and Type
    # We use the institution name + account number/last4 to create a unique, stable name.
    bank_name = getattr(account.statement, 'institution', 'Unknown Bank') or "Unknown Bank"
    acc_number = getattr(account, 'account_id', '0000') or "0000"
    # Sanitize to make a clean ledger account name (e.g., "RBC Visa - 1234")
    safe_acc_name = f"{bank_name} {acc_number[-4:]}"
    account_type = getattr(account, 'type', 'Bank') # 'Bank', 'CreditLine', etc.

    # Map OFX types to Ledger Types
    ledger_acc_type = "Asset" if account_type in ("Bank", "Asset") else "Liability"

    # Check if we need to create this account in the ledger
    missing_accounts = []
    if safe_acc_name not in ledger.accounts:
        missing_accounts.append((safe_acc_name, ledger_acc_type))
        # Auto-create it so we can proceed with the import
        ok, msg = ledger.create_account(safe_acc_name, ledger_acc_type)
        if not ok:
            raise RuntimeError(f"Failed to auto-create account '{safe_acc_name}': {msg}")
        logger.info(f"OFX Import: Auto-created {ledger_acc_type} account '{safe_acc_name}'")

    # Determine the "Uncategorized" counterpart account
    # If it's an Asset account, expenses go to Uncategorized Expense.
    # If it's a Liability account, purchases go to Uncategorized Expense, payments to Uncategorized Income.
    # For simplicity, we'll use "Uncategorized Expense" for outflows and "Uncategorized Income" for inflows.
    uncategorized_expense = "Uncategorized Expense"
    uncategorized_income = "Uncategorized Income"

    for acc_name in [uncategorized_expense, uncategorized_income]:
        if acc_name not in ledger.accounts:
            acc_type = "Expense" if "Expense" in acc_name else "Revenue"
            ledger.create_account(acc_name, acc_type)

    transactions_to_import = []
    skipped = 0

    for txn in account.statement.transactions:
        date_str = txn.date.strftime("%Y-%m-%d")
        description = (txn.memo or txn.payee or "OFX Transaction").strip()
        fitid = getattr(txn, 'id', None) # The crucial anti-duplicate ID

        # Skip if we already have this exact transaction (belt-and-suspenders,
        # ledger.record_transaction also checks this, but good to filter early)
        if fitid and ledger._imported_id_exists(fitid):
            continue

        amount = Decimal(str(txn.amount))
        if amount == 0:
            continue

        tran_type = getattr(txn, 'type', '')
        bank_dr, bank_cr = _parse_ofx_amount(account_type, amount, tran_type)

        # Build the double-entry lines
        lines_data = []
        if bank_dr > 0:
            lines_data.append((safe_acc_name, bank_dr, Decimal("0.00")))
            lines_data.append((uncategorized_income, Decimal("0.00"), bank_dr))
        else:
            lines_data.append((uncategorized_expense, bank_cr, Decimal("0.00")))
            lines_data.append((safe_acc_name, Decimal("0.00"), bank_cr))

        transactions_to_import.append({
            "date": date_str,
            "description": description,
            "lines": lines_data,
            "imported_id": fitid
        })

    logger.info(f"OFX Import: Parsed {len(transactions_to_import)} new transactions from {file_path}. Skipped {skipped} duplicates.")
    return transactions_to_import, missing_accounts
