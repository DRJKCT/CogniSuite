import os
from datetime import datetime

from PyQt6.QtCore import QThread, pyqtSignal

from parser_integration import parse_statement_to_transactions


def normalize_statement_date(
    value: str | None,
    reference_date: datetime | None = None,
) -> str | None:
    """
    Convert common statement date formats to YYYY-MM-DD.
    Returns None if parsing fails.

    Some statement formats (e.g. CIBC's "Jan 5") omit the year. In that
    case we infer the year relative to `reference_date` (defaults to
    today): if treating the date as being in the reference year would
    put it more than ~2 months in the future, we assume it actually
    belongs to the previous year. This handles statements imported
    shortly after year-end, where December transactions would otherwise
    be silently mislabeled as belonging to the new year.
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


class PDFImportThread(QThread):
    progress_signal = pyqtSignal(int, str)
    # imported count, review summary. review summary is a dict:
    # {
    #   "skipped_lines": [{"file": str, "line": str, "reason": str}, ...],
    #   "balance_mismatches": [{"file": str, "date": str, "description": str, "detail": str}, ...],
    #   "unverified_count": int,   # rows we couldn't balance-check at all (e.g. no opening balance found)
    # }
    # Empty lists/zero mean nothing needs review.
    finished_signal = pyqtSignal(int, dict)
    error_signal = pyqtSignal(str)

    def __init__(
        self,
        files,
        ledger,
        bank=None,
        statement_type: str = "auto",
        parent=None,
    ):
        super().__init__(parent)
        self.files = files or []
        self.gl = ledger
        self.bank = bank
        self.statement_type = statement_type

    def run(self):
        try:
            total_files = len(self.files)

            review = {
                "skipped_lines": [],
                "balance_mismatches": [],
                "unverified_count": 0,
            }

            if total_files == 0:
                self.progress_signal.emit(100, "No files selected.")
                self.finished_signal.emit(0, review)
                return

            imported = 0
            # Tracks how many times we've seen a given (date, desc, amount)
            # combo so far in this import run. Deterministic based on the
            # order transactions appear in the statement, so re-importing
            # the same PDF produces the same sequence of imported_ids and
            # still gets deduped -- while two distinct real transactions
            # that share a date/description/amount each get their own id
            # instead of the second one being silently dropped.
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

                # Surface anything the parser couldn't confidently handle,
                # regardless of whether any transactions were found at all
                # -- a statement that produces zero transactions but has
                # unparsed lines is itself a strong signal something's wrong.
                for skipped in diagnostics.get("skipped_lines", []):
                    review["skipped_lines"].append({"file": file_name, **skipped})
                for mismatch in diagnostics.get("balance_mismatches", []):
                    review["balance_mismatches"].append({"file": file_name, **mismatch})

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

                    # Base imported_id: date + description + signed amount
                    # reduces collisions across unrelated transactions
                    # (the sign naturally distinguishes an outflow from an
                    # inflow of the same magnitude on the same day). Append
                    # an occurrence index so multiple *real* same-day/same-
                    # amount/same-description transactions (e.g. two
                    # coffees at the same place) each get a unique id
                    # rather than the second one being rejected as a
                    # duplicate.
                    base_key = (date_str, desc[:40], f"{amount:.2f}")
                    occurrence = occurrence_counts.get(base_key, 0)
                    occurrence_counts[base_key] = occurrence + 1
                    imp_id = f"{base_key[0]}::{base_key[1]}::{base_key[2]}::{occurrence}"

                    # The parser already resolved the correct debit/credit
                    # polarity for this row (a deposit posts the opposite
                    # way from a withdrawal) -- just use it directly rather
                    # than re-deriving it here.
                    lines = tx["lines"]

                    ok, _ = self.gl.record_transaction(
                        date_str,
                        desc,
                        lines,
                        imported_id=imp_id,
                        persist=True,
                    )

                    if ok:
                        imported += 1

                        # If the statement line included the bank's own
                        # running balance (currently only Tangerine bank
                        # statements), record it as a reconciliation
                        # checkpoint so the ledger's computed balance for
                        # this account can later be checked against it.
                        # Only done for newly-posted transactions so
                        # re-importing the same statement doesn't pile up
                        # duplicate checkpoints.
                        statement_balance = tx.get("statement_balance")
                        if statement_balance is not None:
                            self.gl.record_statement_balance(
                                tx["contra_account"],
                                date_str,
                                statement_balance,
                                source=file_name,
                            )

            self.progress_signal.emit(100, "Import complete.")
            self.finished_signal.emit(imported, review)

        except Exception as e:
            self.error_signal.emit(str(e))
