# csv_transaction_import.py
# For CogniFinance -- generic CSV import for investment/crypto
# transactions (buy/sell/dividend/transfer).
#
# Answers open question #4 from the Investment & Crypto planning brief
# ("which brokerage/exchange CSV format(s) to target first") with a
# deliberately format-agnostic first cut rather than a guess at
# Coinbase's or Kraken's exact column layout: a user-configurable
# column mapping, so any export with a date/symbol/action/quantity/
# price shape can be imported today. Dedicated per-exchange parsers
# (which would know a specific provider's quirks -- e.g. Coinbase's
# separate "Fees" column, Kraken's ledger-style paired rows) remain a
# real follow-on decision, not something to guess at here.
#
# Architecture: this file knows NOTHING about investment_engine.py or
# crypto_engine.py. It parses a CSV into ParsedTransactionRow objects
# (pure, no side effects -- same spirit as lot_matcher.py and
# price_history.py's CSV parser) and then, for the write path, calls a
# caller-supplied `poster` callback per row. The poster closure is
# where engine-specific knowledge (which engine, which cash account,
# which wallet, whether to auto-create an unseen holding) lives --
# built by investments_tab.py / crypto_engine's UI, not here. This
# keeps crypto's extra wallet concept from leaking into this shared
# file, per the brief's separation principle.
#
# Follows ofx_import.py's conventions where they transfer directly:
#   - A pure parse function, separate from the QThread that drives it.
#   - A background QThread (progress_signal / finished_signal /
#     error_signal) so a large CSV doesn't freeze the UI, matching
#     OFXImportThread's shape.
#   - Structural problems (unreadable file, missing required columns)
#     raise immediately; row-level problems are skipped and counted,
#     not fatal to the whole import -- OFX's per-transaction "continue"
#     on a duplicate FITID is the analog.
# NOTE: I did not have import_manager.py (which appears to be what
# actually drives OFXImportThread from a QProgressDialog in the real
# app), so the dialog wiring in investments_tab.py is self-contained
# rather than routed through that manager. Worth reconciling with
# import_manager.py's actual pattern if useful for consistency.

import csv
import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import List, Dict, Optional, Callable, Tuple, Any

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit, QLabel, QDialogButtonBox, QComboBox,
)

logger = logging.getLogger(__name__)

# Normalizes a variety of exchange/brokerage action strings to one of
# our four canonical actions. Extend this, not the callers, when a new
# provider's wording shows up.
DEFAULT_ACTION_ALIASES: Dict[str, str] = {
    "buy": "buy", "bought": "buy", "purchase": "buy",
    "sell": "sell", "sold": "sell", "sale": "sell",
    "dividend": "dividend", "div": "dividend", "distribution": "dividend",
    "transfer in": "transfer_in", "receive": "transfer_in", "deposit": "transfer_in",
    "transfer out": "transfer_out", "send": "transfer_out", "withdrawal": "transfer_out",
}

CANONICAL_ACTIONS = ("buy", "sell", "dividend", "transfer_in", "transfer_out")


class CSVImportError(ValueError):
    """Structural problem with the file itself -- unreadable, or
    missing a column the mapping says to expect. Distinct from a
    single bad row, which is skipped rather than raised."""


@dataclass
class ColumnMapping:
    """Which column in the user's CSV holds which field. Only date,
    symbol, and action are required; quantity/price/fee are optional
    per-action (a dividend row has no quantity, for instance). If the
    file gives a total dollar amount instead of a per-unit price,
    supply amount_col instead of price_col and price is derived as
    amount / quantity.
    """
    date_col: str
    symbol_col: str
    action_col: str
    quantity_col: Optional[str] = None
    price_col: Optional[str] = None
    amount_col: Optional[str] = None
    fee_col: Optional[str] = None
    action_aliases: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ACTION_ALIASES))


@dataclass
class ParsedTransactionRow:
    row_number: int  # 1-based, header excluded (row 2 in the file = first data row)
    date: str  # ISO YYYY-MM-DD
    symbol: str
    action: str  # one of CANONICAL_ACTIONS
    quantity: Optional[Decimal]
    price: Optional[Decimal]
    fee: Decimal
    raw: Dict[str, str]  # original row, kept for error messages / audit trail


@dataclass
class ParseResult:
    rows: List[ParsedTransactionRow]
    skipped: List[Tuple[int, str]]  # (row_number, reason)


def _normalize_date(raw: str) -> Optional[str]:
    raw = raw.strip()
    for sep in ("-", "/"):
        if sep not in raw:
            continue
        parts = raw.split(sep)
        if len(parts) != 3:
            continue
        try:
            if len(parts[0]) == 4:
                y, m, d = parts
            elif len(parts[2]) == 4:
                m, d, y = parts
            else:
                continue
            y, m, d = int(y), int(m), int(d)
            return f"{y:04d}-{m:02d}-{d:02d}"
        except ValueError:
            continue
    return None


def _decimal_or_none(raw: Optional[str]) -> Optional[Decimal]:
    if raw is None:
        return None
    raw = raw.strip().replace(",", "").replace("$", "")
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def parse_transaction_csv(path: str, mapping: ColumnMapping) -> ParseResult:
    """Pure parse: no engine, no DB, no side effects. Raises
    CSVImportError for structural problems; skips and records
    individual bad rows rather than failing the whole file."""
    rows: List[ParsedTransactionRow] = []
    skipped: List[Tuple[int, str]] = []

    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                raise CSVImportError(f"'{path}' has no header row.")

            required = [mapping.date_col, mapping.symbol_col, mapping.action_col]
            missing = [c for c in required if c not in reader.fieldnames]
            if missing:
                raise CSVImportError(
                    f"'{path}' is missing required column(s) {missing}. "
                    f"Found columns: {reader.fieldnames}"
                )

            for i, row in enumerate(reader, start=2):
                date_str = _normalize_date(row.get(mapping.date_col, ""))
                symbol = (row.get(mapping.symbol_col) or "").strip().upper()
                action_raw = (row.get(mapping.action_col) or "").strip().lower()
                action = mapping.action_aliases.get(action_raw)

                if date_str is None:
                    skipped.append((i, f"unparseable date '{row.get(mapping.date_col)}'"))
                    continue
                if not symbol:
                    skipped.append((i, "missing symbol"))
                    continue
                if action is None:
                    skipped.append((i, f"unrecognized action '{action_raw}'"))
                    continue

                quantity = _decimal_or_none(row.get(mapping.quantity_col)) if mapping.quantity_col else None
                price = _decimal_or_none(row.get(mapping.price_col)) if mapping.price_col else None
                fee = _decimal_or_none(row.get(mapping.fee_col)) if mapping.fee_col else None
                fee = fee if fee is not None else Decimal("0.00")

                if price is None and mapping.amount_col and quantity:
                    amount = _decimal_or_none(row.get(mapping.amount_col))
                    if amount is not None and quantity != 0:
                        price = amount / quantity

                if action in ("buy", "sell") and (quantity is None or price is None):
                    skipped.append((i, f"'{action}' requires quantity and price/amount; got quantity={quantity}, price={price}"))
                    continue
                if action == "dividend" and price is None and mapping.amount_col:
                    # For dividends we treat "price" loosely as the total
                    # amount when no per-unit price makes sense.
                    price = _decimal_or_none(row.get(mapping.amount_col))
                if action == "dividend" and price is None:
                    skipped.append((i, "'dividend' requires an amount (via price_col or amount_col)"))
                    continue

                rows.append(ParsedTransactionRow(
                    row_number=i, date=date_str, symbol=symbol, action=action,
                    quantity=quantity, price=price, fee=fee, raw=dict(row),
                ))
    except FileNotFoundError:
        raise CSVImportError(f"File not found: '{path}'")

    return ParseResult(rows=rows, skipped=skipped)


# A poster takes one parsed row and attempts to apply it (e.g. calling
# investment_engine.buy(...) or crypto_engine.transfer(...) with
# whatever fixed context -- cash account, wallet, default report line
# -- the caller has already bound into the closure). Returns (ok, msg),
# same convention as the engines' own methods.
PosterFn = Callable[[ParsedTransactionRow], Tuple[bool, str]]


class CSVColumnMappingDialog(QDialog):
    """Lets the user tell us which of their CSV's columns hold which
    field -- the concrete answer to open question #4 (which
    brokerage/exchange format to target first): don't guess at one
    provider's layout, ask once per import. Prefilled with common
    header names as a starting guess; the user edits whatever doesn't
    match their file. Returns a ColumnMapping via get_mapping() if
    accepted, only after the file's own header row is checked against
    it (required column presence) so a bad mapping is caught here,
    before the background thread starts."""

    def __init__(self, csv_path: str, parent=None):
        super().__init__(parent)
        self.csv_path = csv_path
        self.setWindowTitle("Map CSV Columns")
        layout = QVBoxLayout(self)

        try:
            with open(csv_path, newline="", encoding="utf-8-sig") as f:
                header_line = f.readline().strip()
        except Exception:
            header_line = ""
        layout.addWidget(QLabel(f"Detected header row:\n{header_line}"))
        layout.addWidget(QLabel(
            "Enter the exact column names from your file for each field below. "
            "Leave Quantity/Price/Amount/Fee blank if your file doesn't have that column."
        ))

        form = QFormLayout()
        self.date_col = QLineEdit("Date")
        self.symbol_col = QLineEdit("Symbol")
        self.action_col = QLineEdit("Type")
        self.quantity_col = QLineEdit("Quantity")
        self.price_col = QLineEdit("Price")
        self.amount_col = QLineEdit("")
        self.fee_col = QLineEdit("Fee")
        form.addRow("Date column:", self.date_col)
        form.addRow("Symbol column:", self.symbol_col)
        form.addRow("Action/Type column:", self.action_col)
        form.addRow("Quantity column:", self.quantity_col)
        form.addRow("Price/unit column:", self.price_col)
        form.addRow("Total amount column (if no per-unit price):", self.amount_col)
        form.addRow("Fee column:", self.fee_col)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._mapping: Optional[ColumnMapping] = None

    def _on_accept(self):
        mapping = ColumnMapping(
            date_col=self.date_col.text().strip(),
            symbol_col=self.symbol_col.text().strip(),
            action_col=self.action_col.text().strip(),
            quantity_col=self.quantity_col.text().strip() or None,
            price_col=self.price_col.text().strip() or None,
            amount_col=self.amount_col.text().strip() or None,
            fee_col=self.fee_col.text().strip() or None,
        )
        # Fail fast on a structurally bad mapping (missing required
        # column) rather than letting the background thread discover
        # it -- same "catch it before the thread starts" spirit as
        # inspect_ofx_account() running before import_ofx_transactions().
        try:
            with open(self.csv_path, newline="", encoding="utf-8-sig") as f:
                header = next(csv.reader(f), [])
        except Exception as e:
            QLabel(f"Could not re-read file: {e}")
            self.reject()
            return
        required = [mapping.date_col, mapping.symbol_col, mapping.action_col]
        missing = [c for c in required if c not in header]
        if missing:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Missing columns", f"Column(s) not found in file: {missing}")
            return
        self._mapping = mapping
        self.accept()

    def get_mapping(self) -> Optional[ColumnMapping]:
        return self._mapping


class TransactionCSVImportThread(QThread):
    """Background import, mirroring OFXImportThread's shape: parse,
    then apply row-by-row with progress reporting, collecting
    successes/skips/errors rather than failing all-or-nothing on one
    bad row. Structural parse failures (CSVImportError) still abort
    immediately via error_signal, same as OFX's statement-level
    validation failures."""

    progress_signal = pyqtSignal(int, str)
    finished_signal = pyqtSignal(int, dict)  # (imported_count, {"skipped": [...], "errors": [...]})
    error_signal = pyqtSignal(str)

    def __init__(self, file_path: str, mapping: ColumnMapping, poster: PosterFn, parent=None):
        super().__init__(parent)
        self.file_path = file_path
        self.mapping = mapping
        self.poster = poster

    def run(self):
        try:
            self.progress_signal.emit(0, f"Parsing {self.file_path}...")
            try:
                result = parse_transaction_csv(self.file_path, self.mapping)
            except CSVImportError as e:
                self.error_signal.emit(str(e))
                return

            skipped_details = [f"Row {n}: {reason}" for n, reason in result.skipped]
            errors: List[str] = []
            imported = 0
            total = len(result.rows)

            for j, row in enumerate(result.rows):
                pct = int(((j + 1) / total) * 100) if total else 100
                self.progress_signal.emit(pct, f"Row {row.row_number}: {row.action} {row.symbol}")
                try:
                    ok, msg = self.poster(row)
                except Exception as e:
                    ok, msg = False, str(e)
                if ok:
                    imported += 1
                else:
                    errors.append(f"Row {row.row_number} ({row.action} {row.symbol}): {msg}")

            self.progress_signal.emit(100, "Import complete.")
            self.finished_signal.emit(imported, {"skipped": skipped_details, "errors": errors})
        except Exception as e:
            self.error_signal.emit(str(e))
