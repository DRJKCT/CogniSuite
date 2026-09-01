import os
import logging
from decimal import Decimal
from typing import List, Tuple, Dict, Any, Optional
from ofxparse import OfxParser
from reconciliation_matcher import extract_suggested_matches
from PyQt6.QtCore import QThread, pyqtSignal

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
    bank_name = getattr(statement, 'institution', None) or "Unknown Bank"
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
    invert_signs: bool = False,
    check_duplicates: bool = True,
    auto_skip_green: bool = True,
    auto_skip_yellow: bool = False
) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]], Dict[str, Any]]:
    """
    Parse OFX file and return transactions ready for import.

    Returns:
        Tuple of (transactions_to_import, new_accounts_created, duplicate_details)
    """
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


    unique_txns, suggested_matches = extract_suggested_matches(txns, ledger)

    return unique_txns, new_accounts, suggested_matches

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
        rule_category = ledger.apply_category_rules(description)
        lines_data = []

        if is_asset:
            if raw_amount > 0:
                category = rule_category or uncategorized_income
                lines_data.append((target_account_name, abs_amount, Decimal("0.00")))
                lines_data.append((category, Decimal("0.00"), abs_amount))
            else:
                category = rule_category or uncategorized_expense
                lines_data.append((category, abs_amount, Decimal("0.00")))
                lines_data.append((target_account_name, Decimal("0.00"), abs_amount))
        else:
            if raw_amount < 0:
                category = rule_category or uncategorized_expense
                lines_data.append((category, abs_amount, Decimal("0.00")))
                lines_data.append((target_account_name, Decimal("0.00"), abs_amount))
            else:
                category = rule_category or uncategorized_transfer
                # Fixed double-credit bug from original code: DR category, CR target
                lines_data.append((category, abs_amount, Decimal("0.00")))
                lines_data.append((target_account_name, Decimal("0.00"), abs_amount))

        transactions_to_import.append({
            "date": date_str,
            "description": description,
            "lines": lines_data,
            "imported_id": fitid
        })

    ending_bal = getattr(statement, 'balance', None)
    starting_bal = getattr(statement, 'start_balance', None)

    if ending_bal is not None and starting_bal is not None:
        expected_delta = Decimal(str(ending_bal)) - Decimal(str(starting_bal))
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

class OFXImportThread(QThread):
    """Runs the heavy OFX parsing and DB insertion in a background thread."""
    progress_signal = pyqtSignal(int, str)
    finished_signal = pyqtSignal(int, dict)  # (imported_count, review_dict)
    error_signal = pyqtSignal(str)

    def __init__(self, files, ledger, parent=None):
        super().__init__(parent)
        self.files = files or []
        self.gl = ledger

    def run(self):
        try:
            total_files = len(self.files)
            if total_files == 0:
                self.progress_signal.emit(100, "No files selected.")
                self.finished_signal.emit(0, {"suggested_matches": [], "skipped_count": 0, "new_accounts": []})
                return

            total_imported = 0
            total_skipped = 0
            all_suggested_matches = []
            all_new_accounts = []

            for i, file_path in enumerate(self.files):
                file_name = os.path.basename(file_path)
                base_pct = int((i / total_files) * 100)
                self.progress_signal.emit(base_pct, f"Parsing {file_name}...")

                try:
                    txns, new_accounts, suggested_matches = parse_ofx_to_ledger_transactions(file_path, self.gl)
                    all_suggested_matches.extend(suggested_matches)
                    if new_accounts:
                        all_new_accounts.extend(new_accounts)

                    if not txns:
                        continue

                    total_txs = len(txns)
                    for j, tx in enumerate(txns):
                        sub_pct = base_pct + int(((j + 1) / total_txs) * (100 / total_files))
                        self.progress_signal.emit(min(sub_pct, 99), f"Recording {j + 1}/{total_txs} - {file_name}")

                        ok, msg = self.gl.record_transaction(
                            date=tx["date"],
                            description=tx["description"],
                            lines_data=tx["lines"],
                            imported_id=tx["imported_id"],
                            persist=True
                        )
                        if ok:
                            total_imported += 1
                        else:
                            logger.warning(f"OFX Import: Ledger rejected '{tx['description']}': {msg}")
                            total_skipped += 1
                except Exception as e:
                    logger.error(f"OFX Import failed for {file_name}: {e}")
                    # Emit error but continue to next file if possible, or abort.
                    # For simplicity, we'll emit the error and abort the thread.
                    self.error_signal.emit(f"Error processing {file_name}:\n{e}")
                    return

            self.progress_signal.emit(100, "Import complete.")
            review = {
                "suggested_matches": all_suggested_matches,
                "skipped_count": total_skipped,
                "new_accounts": all_new_accounts
            }
            self.finished_signal.emit(total_imported, review)
        except Exception as e:
            self.error_signal.emit(str(e))
