import os
import logging
from decimal import Decimal
from typing import List, Tuple, Dict, Any, Optional
from ofxparse import OfxParser

logger = logging.getLogger(__name__)

def inspect_ofx_account(file_path: str) -> Dict[str, Any]:
    """Reads OFX metadata without importing transactions."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"OFX file not found: {file_path}")

    with open(file_path, 'rb') as f:
        ofx = OfxParser.parse(f)

    account = ofx.account
    if not account:
        raise ValueError("No account data found in the OFX file.")

    is_cc = account.__class__.__name__ == 'CreditCardAccount'
    raw_type = str(getattr(account, 'type', '') or '').strip().upper()

    liability_keywords = ("CREDIT", "CC", "CREDITLINE", "LOAN", "MORTGAGE", "LIABILITY")

    if is_cc or any(kw in raw_type for kw in liability_keywords):
        is_asset = False
    else:
        asset_types = ("0", "1", "2", "CHECKING", "SAVINGS", "MONEYMRKT", "BANK", "ASSET")
        is_asset = raw_type in asset_types

    statement = getattr(account, 'statement', None)
    bank_name = getattr(statement, 'institution', None) or "Unknown Bank" or "Unknown Bank"
    raw_acc_id = str(getattr(account, 'account_id', '0000') or "0000")
    clean_acc_id = raw_acc_id.strip()
    suffix = clean_acc_id[-4:] if len(clean_acc_id) >= 4 else clean_acc_id.zfill(4)

    suggested_name = f"{bank_name} {suffix}"

    return {
        "raw_account_id": clean_acc_id,
        "bank_name": bank_name,
        "suggested_name": suggested_name,
        "is_asset": is_asset,
        "detected_type": "Asset" if is_asset else "Liability"
    }

def parse_ofx_to_ledger_transactions(
    file_path: str,
    ledger,
    invert_signs: bool = False
) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
    account_info = inspect_ofx_account(file_path)
    target_account_name = account_info["suggested_name"]
    is_asset = account_info["is_asset"]

    new_accounts = []
    if target_account_name not in ledger.accounts:
        ledger.create_account(target_account_name, account_info["detected_type"])
        new_accounts.append((target_account_name, account_info["detected_type"]))

    txns = import_ofx_transactions(
        file_path=file_path,
        ledger=ledger,
        target_account_name=target_account_name,
        is_asset=is_asset,
        invert_signs=invert_signs
    )

    return txns, new_accounts

def import_ofx_transactions(
    file_path: str,
    ledger,
    target_account_name: str,
    is_asset: bool,
    invert_signs: bool = False
) -> List[Dict[str, Any]]:
    with open(file_path, 'rb') as f:
        ofx = OfxParser.parse(f)

    account = getattr(ofx, 'account', None)
    if not account:
        raise ValueError("No account data found in the OFX file.")

    statement = getattr(account, 'statement', None)
    if not statement:
        raise ValueError("No statement data found in the OFX account.")

    # Ensure system default clearing and category accounts exist
    uncategorized_expense = "Uncategorized Expense"
    uncategorized_income = "Uncategorized Income"
    uncategorized_transfer = "Uncategorized Transfer"

    defaults = [
        (uncategorized_expense, "Expense"),
        (uncategorized_income, "Revenue"),
        (uncategorized_transfer, "Asset")
    ]

    for acc_name, acc_type in defaults:
        if acc_name not in ledger.accounts:
            ledger.create_account(acc_name, acc_type)

    transactions_to_import = []

    for txn in statement.transactions:
        date_str = txn.date.strftime("%Y-%m-%d")

        # 1. Description Fix: Prioritize payee over memo, merge if distinct
        payee = str(getattr(txn, 'payee', '') or '').strip()
        memo = str(getattr(txn, 'memo', '') or '').strip()

        if payee and memo and payee.lower() != memo.lower():
            description = f"{payee} ({memo})"
        else:
            description = payee or memo or "OFX Transaction"

        fitid = getattr(txn, 'id', None)

        if fitid and ledger._imported_id_exists(fitid):
            continue

        raw_amount = Decimal(str(txn.amount))
        if raw_amount == 0:
            continue

        if invert_signs:
            raw_amount = -raw_amount

        abs_amount = abs(raw_amount)
        lines_data = []

        # 2. Double-Entry Posting Rules
        if is_asset:
            if raw_amount > 0:  # Deposit (Increases Asset -> DEBIT)
                lines_data.append((target_account_name, abs_amount, Decimal("0.00")))
                lines_data.append((uncategorized_income, Decimal("0.00"), abs_amount))
            else:  # Withdrawal / Payment (Decreases Asset -> CREDIT)
                lines_data.append((uncategorized_expense, abs_amount, Decimal("0.00")))
                lines_data.append((target_account_name, Decimal("0.00"), abs_amount))
        else:
            # Liability / Credit Card Account
            if raw_amount < 0:  # Purchase (Increases Liability -> CREDIT)
                lines_data.append((uncategorized_expense, abs_amount, Decimal("0.00")))
                lines_data.append((target_account_name, Decimal("0.00"), abs_amount))
            else:  # Payment Received (Decreases Liability -> DEBIT)
                lines_data.append((target_account_name, Decimal("0.00"), abs_amount))
                lines_data.append((uncategorized_transfer, Decimal("0.00"), abs_amount))

        transactions_to_import.append({
            "date": date_str,
            "description": description,
            "lines": lines_data,
            "imported_id": fitid
        })

    # 3. Post-Mapping Balance Assertion Fix
    ending_bal = getattr(statement, 'balance', None)
    starting_bal = getattr(statement, 'start_balance', None)

    if ending_bal is not None and starting_bal is not None:
        expected_delta = Decimal(str(ending_bal)) - Decimal(str(starting_bal))

        # Sum generated debit/credit postings specifically for the target account
        actual_mapped_delta = Decimal("0.00")
        for t in transactions_to_import:
            for acc, debit, credit in t["lines"]:
                if acc == target_account_name:
                    actual_mapped_delta += (debit - credit) if is_asset else (credit - debit)

        if actual_mapped_delta != expected_delta:
            raise ValueError(
                f"OFX Import Validation Failed for '{target_account_name}': "
                f"Statement delta is {expected_delta}, but generated postings net to {actual_mapped_delta}. "
                f"Check invert_signs flag or sign mapping rules."
            )

    return transactions_to_import
