import os
import logging
from decimal import Decimal
from typing import List, Tuple, Dict, Any, Optional
from ofxparse import OfxParser
from reconciliation_matcher import extract_suggested_matches
from merchant_normalization import normalize_merchant
from PyQt6.QtCore import QThread, pyqtSignal
from currency import normalize_currency

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
    # The statement's currency (OFX CURDEF). None when the file doesn't say.
    raw_ccy = str(getattr(account, 'curdef', '') or getattr(statement, 'currency', '') or '').strip()
    try:
        currency = normalize_currency(raw_ccy) if raw_ccy else None
    except ValueError:
        currency = None
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
        "detected_type": "Asset" if is_asset else "Liability",
        "currency": currency,
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
    ofx_currency = account_info.get("currency")
    if target_account_name not in ledger.accounts:
        # A statement in a non-base currency creates a foreign-currency account.
        ledger.create_account(target_account_name, account_info["detected_type"], currency=ofx_currency)
        new_accounts.append((target_account_name, account_info["detected_type"]))
    elif ofx_currency and ofx_currency != ledger.accounts[target_account_name].currency:
        raise ValueError(
            f"'{os.path.basename(file_path)}' is a {ofx_currency} statement, but account "
            f"'{target_account_name}' is in {ledger.accounts[target_account_name].currency}. "
            "Importing would post the wrong currency; rename or recreate the account."
        )

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

    # Statement amounts are in the account's own currency. For a foreign
    # account each one is converted to base at its transaction date's rate,
    # and the native amount + rate ride along in tx["fx"] (see lines_with_fx).
    target_acc = ledger.accounts.get(target_account_name)
    foreign = target_acc is not None and target_acc.currency != ledger.base_currency
    rate_errors: Dict[str, str] = {}

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
        fx_map: Dict[str, Any] = {}
        if foreign:
            try:
                abs_amount, target_fx = ledger.convert_to_base(target_account_name, abs_amount, date_str)
            except ValueError as e:
                rate_errors.setdefault(date_str, str(e))
                continue
            fx_map[target_account_name] = target_fx
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

        merchant_key, merchant_display = normalize_merchant(description)
        category_suggested = None
        if merchant_key:
            suggestion = ledger.get_best_suggestion_for_merchant(merchant_key)
            if suggestion:
                category_suggested = suggestion["category"]

        transactions_to_import.append({
            "date": date_str,
            "description": description,
            "lines": lines_data,
            "fx": fx_map,
            "imported_id": fitid,
            "merchant_key": merchant_key,
            "merchant_display": merchant_display,
            "category_suggested": category_suggested,
        })

    if rate_errors:
        first = sorted(rate_errors)[0]
        raise ValueError(
            f"Can't import '{target_account_name}': {len(rate_errors)} date(s) have no exchange rate "
            f"(first: {first}). {rate_errors[first]} Nothing was imported."
        )

    ending_bal = getattr(statement, 'balance', None)
    starting_bal = getattr(statement, 'start_balance', None)

    if ending_bal is not None and starting_bal is not None:
        expected_delta = Decimal(str(ending_bal)) - Decimal(str(starting_bal))
        actual_mapped_delta = Decimal("0.00")
        for t in transactions_to_import:
            for acc, debit, credit in t["lines"]:
                if acc == target_account_name:
                    # The statement is in the account's own currency, so compare native amounts.
                    fx = (t.get("fx") or {}).get(acc)
                    if fx is not None:
                        debit = fx.native if debit > 0 else Decimal("0.00")
                        credit = fx.native if credit > 0 else Decimal("0.00")
                    actual_mapped_delta += (debit - credit) if is_asset else (credit - debit)

        if actual_mapped_delta != expected_delta:
            raise ValueError(
                f"OFX Import Validation Failed for '{target_account_name}': "
                f"Statement delta is {expected_delta}, but generated postings net to {actual_mapped_delta}. "
                f"Check invert_signs flag or sign mapping rules."
            )

    return transactions_to_import

def lines_with_fx(tx: Dict[str, Any]) -> list:
    """The ledger-ready lines for a parsed import dict. Lines stay plain
    (account, dr, cr) base-currency tuples in tx["lines"] -- so existing
    consumers such as reconciliation_matcher are unaffected -- and any
    foreign-currency detail sits in tx["fx"] ({account_name: FxInfo}).
    This attaches it for posting. A dict with no "fx" returns its lines as-is."""
    lines = tx.get("lines") or []
    fx_map = tx.get("fx") or {}
    if not fx_map:
        return lines
    return [(acc, dr, cr, fx_map.get(acc)) for acc, dr, cr in lines]


def record_merchant_learning_for_last_transaction(ledger, description: str, merchant_key: str = None,
                                                   merchant_display: str = None, category_suggested=None,
                                                   category_line_index: int = 0) -> None:
    """
    Best-effort: stage a merchant_draft for the category leg of the
    transaction that was JUST posted via ledger.record_transaction().
    Must be called immediately after a successful, non-batched post --
    see GeneralLedger.get_last_transaction() for why.

    merchant_key/merchant_display/category_suggested are optional: if not
    supplied (e.g. by a caller that doesn't have them precomputed, such
    as a rejected-match repost whose dict shape isn't guaranteed to carry
    them), they're derived here from `description` alone -- normalize_merchant
    is a pure function of the description string, so recomputing it is
    always safe and always gives the same answer.

    category_line_index assumes the double-entry convention used
    throughout this codebase's importers: the category/expense leg is
    lines_data[0], the bank/asset leg is lines_data[1]. If a future
    importer builds lines in the other order, pass the correct index.

    Never raises: merchant learning is a passive side channel and must
    never be able to fail an import that otherwise succeeded.
    """
    try:
        tx = ledger.get_last_transaction()
        if tx is None or not tx.lines or category_line_index >= len(tx.lines):
            return
        line = tx.lines[category_line_index]
        if line.id is None:
            return

        if merchant_key is None:
            merchant_key, merchant_display = normalize_merchant(description)
        if not merchant_key:
            return

        if category_suggested is None:
            suggestion = ledger.get_best_suggestion_for_merchant(merchant_key)
            category_suggested = suggestion["category"] if suggestion else None

        ledger.record_merchant_draft(
            tx_id=tx.id,
            line_id=line.id,
            raw_string=description,
            merchant_key=merchant_key,
            merchant_display=merchant_display,
            category_suggested=category_suggested,
        )
    except Exception:
        logger.exception("Merchant learning: failed to stage draft (non-fatal)")


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
                            lines_data=lines_with_fx(tx),
                            imported_id=tx["imported_id"],
                            persist=True
                        )
                        if ok:
                            total_imported += 1
                            record_merchant_learning_for_last_transaction(
                                self.gl,
                                tx["description"],
                                merchant_key=tx.get("merchant_key"),
                                merchant_display=tx.get("merchant_display"),
                                category_suggested=tx.get("category_suggested"),
                            )
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
