import os
import logging
from datetime import datetime
from decimal import Decimal

from PyQt6.QtCore import QThread, pyqtSignal

from parser_integration import parse_statement_to_transactions
from reconciliation_matcher import extract_suggested_matches

logger = logging.getLogger(__name__)


def normalize_statement_date(
    value: str | None,
    reference_date: datetime | None = None,
) -> str | None:
    """
    Convert common statement date formats to YYYY-MM-DD.
    Returns None if parsing fails.
    """
    if not value:
        return None

    value = value.strip()
    ref = reference_date or datetime.now()

    formats = [
        "%Y-%m-%d",
        "%d-%b-%Y",
        "%d %b %Y",
        "%b %d %Y",
        "%b %d",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(value, fmt)

            # For formats like "Jan 5" with no year
            if dt.year == 1900:
                candidate = dt.replace(year=ref.year)
                if (candidate - ref).days > 60:
                    candidate = candidate.replace(year=ref.year - 1)
                dt = candidate

            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    return None

def lines_for_posting(tx: dict, ledger, date_str: str) -> list:
    """
    Ledger-ready lines for one parsed statement transaction.

    * tx["fx"] ({account_name: FxInfo}, optional -- a parser that already
      knows a line's native amount and rate can supply it) is attached as-is.
    * Otherwise, if exactly one line of a TWO-line entry posts to a
      foreign-currency account, the statement amounts are in that
      account's own currency: the amount is converted at the rate for
      `date_str` and BOTH lines are restated in base, with the native
      detail kept on the foreign line.
    * Base-currency entries are returned untouched.

    Raises ValueError (no rate on file, or a shape we can't convert safely)
    for the caller to record against that one transaction.
    """
    lines = tx["lines"]
    fx_map = tx.get("fx") or {}
    if fx_map:
        return [(acc, dr, cr, fx_map.get(acc)) for acc, dr, cr in lines]

    foreign = [i for i, (acc, _dr, _cr) in enumerate(lines)
               if acc in ledger.accounts and ledger.accounts[acc].currency != ledger.base_currency]
    if not foreign:
        return lines
    if len(lines) != 2 or len(foreign) != 1:
        raise ValueError(
            "Only simple two-line statement entries can post to a foreign-currency account; "
            "enter this one manually."
        )
    idx = foreign[0]
    acc, dr, cr = lines[idx]
    dr, cr = Decimal(str(dr)), Decimal(str(cr))
    base_amount, fx = ledger.convert_to_base(acc, dr if dr > 0 else cr, date_str)
    zero = Decimal("0.00")
    out = []
    for i, (a, d, c) in enumerate(lines):
        out.append((a,
                    base_amount if Decimal(str(d)) > 0 else zero,
                    base_amount if Decimal(str(c)) > 0 else zero,
                    fx if i == idx else None))
    return out


class PDFImportThread(QThread):
    progress_signal = pyqtSignal(int, str)
    finished_signal = pyqtSignal(int, dict)
    error_signal = pyqtSignal(str)

    def __init__(
        self,
        files,
        ledger,
        bank=None,
        statement_type: str = "auto",
        parent=None,
        check_duplicates: bool = True,
        auto_skip_green: bool = True,
        auto_skip_yellow: bool = False
    ):
        super().__init__(parent)
        self.files = files or []
        self.gl = ledger
        self.bank = bank
        self.statement_type = statement_type
        self.check_duplicates = check_duplicates
        self.auto_skip_green = auto_skip_green
        self.auto_skip_yellow = auto_skip_yellow

    def run(self):
        try:
            total_files = len(self.files)
            review = {
                "skipped_lines": [],
                "balance_mismatches": [],
                "unverified_count": 0,
                "suggested_matches": [],  # NEW: Track duplicates
                "rejected_by_ledger": [],  # transactions the ledger refused (e.g. no exchange rate)
            }

            if total_files == 0:
                self.progress_signal.emit(100, "No files selected.")
                self.finished_signal.emit(0, review)
                return

            imported = 0
            occurrence_counts: dict[tuple[str, str, str], int] = {}

            for i, pdf_path in enumerate(self.files):
                file_name = os.path.basename(pdf_path)
                base_pct = int((i / total_files) * 100)

                self.progress_signal.emit(base_pct, f"Parsing {file_name}...")

                txs, diagnostics = parse_statement_to_transactions(
                    pdf_path,
                    self.gl,
                    bank=self.bank,
                    statement_type=self.statement_type,
                )

                for skipped in diagnostics.get("skipped_lines", []):
                    review["skipped_lines"].append({"file": file_name, **skipped})
                for mismatch in diagnostics.get("balance_mismatches", []):
                    review["balance_mismatches"].append({"file": file_name, **mismatch})

                if not txs:
                    continue

                unique_txs, suggested_matches = extract_suggested_matches(txs, self.gl)
                for match in suggested_matches:
                    review["suggested_matches"].append({
                        "file": file_name,
                        **match
                    })
                txs = unique_txs

                if not txs:
                    continue

                total_txs = len(txs)

                for j, tx in enumerate(txs):
                    sub_pct = base_pct + int(((j + 1) / total_txs) * (100 / total_files))

                    self.progress_signal.emit(
                        min(sub_pct, 99),
                        f"Recording transaction {j + 1}/{total_txs} - {file_name}",
                    )

                    raw_date = tx.get("date")
                    date_str = normalize_statement_date(raw_date)
                    if not date_str:
                        date_str = datetime.now().strftime("%Y-%m-%d")

                    desc = tx["description"]
                    amount = tx["amount"]

                    if tx.get("balance_check") == "unverified":
                        review["unverified_count"] += 1

                    base_key = (date_str, desc[:40], f"{amount:.2f}")
                    occurrence = occurrence_counts.get(base_key, 0)
                    occurrence_counts[base_key] = occurrence + 1
                    imp_id = f"{base_key[0]}::{base_key[1]}::{base_key[2]}::{occurrence}"

                    try:
                        lines = lines_for_posting(tx, self.gl, date_str)
                        ok, msg = self.gl.record_transaction(
                            date_str, desc, lines, imported_id=imp_id, persist=True
                        )
                    except ValueError as e:
                        ok, msg = False, str(e)
                    if not ok:
                        logger.warning(f"PDF Import: ledger rejected '{desc}' ({date_str}): {msg}")
                        review["rejected_by_ledger"].append(
                            {"file": file_name, "date": date_str, "description": desc, "reason": msg}
                        )

                    if ok:
                        imported += 1
                        statement_balance = tx.get("statement_balance")
                        if statement_balance is not None:
                            self.gl.record_statement_balance(
                                tx["contra_account"], date_str, statement_balance, source=file_name
                            )

            self.progress_signal.emit(100, "Import complete.")
            self.finished_signal.emit(imported, review)

        except Exception as e:
            self.error_signal.emit(str(e))
