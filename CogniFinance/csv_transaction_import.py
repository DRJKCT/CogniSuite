# csv_transaction_import.py
# For CogniFinance -- generic CSV import for investment/crypto
# transactions (buy/sell/dividend/reinvested dividend/split/
# staking-reward-airdrop/transfer).
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
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import List, Dict, Optional, Callable, Tuple, Any

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit, QLabel, QDialogButtonBox, QComboBox,
)

logger = logging.getLogger(__name__)

# Normalizes a variety of exchange/brokerage action strings to one of
# our canonical actions. Extend this, not the callers, when a new
# provider's wording shows up.
# Deliberately NOT aliased: a bare "reinvestment" / "reinvest". Many
# brokers export a reinvested dividend as TWO rows -- a dividend
# received, then a reinvestment purchase. Treating that second row as
# a "drip" (which itself records the dividend as income) would count
# the dividend twice. Only labels that unambiguously describe the
# whole dividend-plus-purchase in ONE row map to "drip"; anything else
# shows up as an unrecognized action so the user decides.
DEFAULT_ACTION_ALIASES: Dict[str, str] = {
    "buy": "buy", "bought": "buy", "purchase": "buy",
    "sell": "sell", "sold": "sell", "sale": "sell",
    "dividend": "dividend", "div": "dividend", "distribution": "dividend",
    "transfer in": "transfer_in", "receive": "transfer_in", "deposit": "transfer_in",
    "transfer out": "transfer_out", "send": "transfer_out", "withdrawal": "transfer_out",
    "drip": "drip", "dividend reinvested": "drip", "dividend reinvestment": "drip",
    "reinvested dividend": "drip", "reinvest dividend": "drip",
    "split": "split", "stock split": "split", "forward split": "split", "reverse split": "split",
    "staking": "staking", "staking reward": "staking", "staking income": "staking",
    "reward": "reward", "rewards": "reward",
    "airdrop": "airdrop",
}

CANONICAL_ACTIONS = (
    "buy", "sell", "dividend", "transfer_in", "transfer_out",
    "drip", "split", "staking", "reward", "airdrop",
)

# Posting order within a single date (lower first). A split must land
# before any sale on its date, because the engine refuses a split once
# a sale on/after its date exists; a buy must land before a sale so
# there is a lot to sell. Everything else is order-insensitive.
_SAME_DAY_ORDER = {"split": 0, "sell": 2, "transfer_out": 2}


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
    # Only used by "split" rows: one column holding the ratio as
    # "2:1", "2-for-1", "2/1" or a bare "2" (meaning 2-for-1). Placed
    # after the older fields so existing keyword/positional construction is unaffected.
    ratio_col: Optional[str] = None
    # Optional multi-currency columns. currency_col: a 3-letter code (USD, EUR...)
    # naming the currency the row's price/amount/fee are in. fx_rate_col: base
    # units per 1 unit of that currency, if the file records the rate it used.
    currency_col: Optional[str] = None
    fx_rate_col: Optional[str] = None


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
    # Added for drip/split rows; all default to None so older callers
    # and tests constructing rows by hand are unaffected.
    amount: Optional[Decimal] = None  # drip: total dividend reinvested; other rows: raw amount column if mapped
    split_new: Optional[Decimal] = None  # split only: new shares ...
    split_old: Optional[Decimal] = None  # ... for this many old shares
    # Multi-currency (None = not given; the poster decides what that means,
    # normally "the cash account's currency"). fx_rate is base units per 1
    # unit of `currency` and is only meaningful together with it.
    currency: Optional[str] = None
    fx_rate: Optional[Decimal] = None


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
    raw = re.sub(r"[$\u20ac\u00a3\u00a5]", "", raw.strip().replace(",", ""))
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None

_CCY_RE = re.compile(r"^[A-Z]{3}$")

_SPLIT_RATIO_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(?:[:/]|-?for-?)\s*(\d+(?:\.\d+)?)\s*$", re.IGNORECASE
)
_BARE_NUMBER_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*$")


def parse_split_ratio(raw: Optional[str]) -> Optional[Tuple[Decimal, Decimal]]:
    """Parse a split ratio into (new_shares, old_shares). Accepts
    "2:1", "2/1", "2-for-1", "2 for 1", and a bare "2" (= 2-for-1);
    a 1-for-10 reverse split is "1:10" / "1-for-10". Returns None for
    anything else, including a zero side or a 1:1 ratio. A hyphen
    between bare numbers ("2-1") is deliberately NOT accepted -- it is
    indistinguishable from a date fragment."""
    if raw is None:
        return None
    m = _SPLIT_RATIO_RE.match(raw)
    if m:
        new, old = Decimal(m.group(1)), Decimal(m.group(2))
    else:
        m = _BARE_NUMBER_RE.match(raw)
        if not m:
            return None
        new, old = Decimal(m.group(1)), Decimal(1)
    if new <= 0 or old <= 0 or new == old:
        return None
    return new, old


def order_rows_for_posting(rows: List[ParsedTransactionRow]) -> List[ParsedTransactionRow]:
    """Chronological order, regardless of how the file was sorted
    (many exports are newest-first). Within one date: splits first,
    then buys/receipts/income, then sells/transfers-out; otherwise
    file order. Posting a sale before the purchase it consumes, or a
    split after a later sale, fails or silently mis-matches lots."""
    return sorted(rows, key=lambda r: (r.date, _SAME_DAY_ORDER.get(r.action, 1), r.row_number))

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

                currency = None
                if mapping.currency_col:
                    ccy_raw = (row.get(mapping.currency_col) or "").strip().upper()
                    if ccy_raw:
                        if not _CCY_RE.match(ccy_raw):
                            skipped.append((i, f"unrecognized currency '{ccy_raw}' (expected a 3-letter code such as USD)"))
                            continue
                        currency = ccy_raw
                fx_rate = None
                if mapping.fx_rate_col:
                    rate_raw = (row.get(mapping.fx_rate_col) or "").strip()
                    if rate_raw:
                        fx_rate = _decimal_or_none(rate_raw)
                        if fx_rate is None or fx_rate <= 0:
                            skipped.append((i, f"invalid exchange rate '{rate_raw}'"))
                            continue

                raw_amount = _decimal_or_none(row.get(mapping.amount_col)) if mapping.amount_col else None
                if price is None and raw_amount is not None and quantity:
                    if quantity != 0:
                        price = raw_amount / quantity

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

                row_amount = raw_amount
                split_new = split_old = None

                if action == "drip":
                    # One-row reinvested dividend: income AND a new lot.
                    # Prefer the broker-reported total (sign-insensitive:
                    # exports often show the reinvestment as a negative
                    # cash movement); fall back to per-unit price x qty.
                    if quantity is None or quantity <= 0:
                        skipped.append((i, "'drip' requires a positive quantity (shares received)"))
                        continue
                    if raw_amount is not None:
                        row_amount = abs(raw_amount)
                    elif price is not None:
                        row_amount = price * quantity
                    else:
                        skipped.append((i, "'drip' requires a dividend amount (amount_col) or a per-unit price"))
                        continue
                    if row_amount <= 0:
                        skipped.append((i, "'drip' amount must be positive"))
                        continue

                if action in ("staking", "reward", "airdrop"):
                    # Income at fair market value on receipt; that value
                    # is also the new lot's cost basis, so a missing or
                    # zero price must NOT silently become a zero basis.
                    if quantity is None or quantity <= 0 or price is None or price <= 0:
                        skipped.append((i, (
                            f"'{action}' requires a positive quantity and a positive fair-market "
                            f"price/amount; got quantity={quantity}, price={price}"
                        )))
                        continue

                if action == "split":
                    if not mapping.ratio_col:
                        skipped.append((i, "'split' rows need a split-ratio column; map one, or use the Stock Split form"))
                        continue
                    ratio_text = row.get(mapping.ratio_col)
                    ratio = parse_split_ratio(ratio_text)
                    if ratio is None:
                        skipped.append((i, f"unparseable split ratio '{ratio_text}' (expected e.g. 2:1, 2-for-1, 1:10)"))
                        continue
                    split_new, split_old = ratio

                rows.append(ParsedTransactionRow(
                    row_number=i, date=date_str, symbol=symbol, action=action,
                    quantity=quantity, price=price, fee=fee, raw=dict(row),
                    amount=row_amount, split_new=split_new, split_old=split_old,
                    currency=currency, fx_rate=fx_rate,
                ))
    except FileNotFoundError:
        raise CSVImportError(f"File not found: '{path}'")

    return ParseResult(rows=rows, skipped=skipped)


# Multi-currency note for poster authors: row.currency (when the file has
# one) names the currency of the row's price/amount/fee. The engines take
# those amounts in the CASH ACCOUNT's currency, so the poster should pick the
# cash account whose currency matches row.currency, and pass row.fx_rate
# (fx_rate=) only when the two agree -- a rate quoted for one currency must
# never be applied to another.
#
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
            "Leave Quantity/Price/Amount/Fee/Split ratio/Currency/Exchange rate blank if your file doesn't have that column."
        ))

        form = QFormLayout()
        self.date_col = QLineEdit("Date")
        self.symbol_col = QLineEdit("Symbol")
        self.action_col = QLineEdit("Type")
        self.quantity_col = QLineEdit("Quantity")
        self.price_col = QLineEdit("Price")
        self.amount_col = QLineEdit("")
        self.fee_col = QLineEdit("Fee")
        self.ratio_col = QLineEdit("")
        self.currency_col = QLineEdit("")
        self.fx_rate_col = QLineEdit("")
        form.addRow("Date column:", self.date_col)
        form.addRow("Symbol column:", self.symbol_col)
        form.addRow("Action/Type column:", self.action_col)
        form.addRow("Quantity column:", self.quantity_col)
        form.addRow("Price/unit column:", self.price_col)
        form.addRow("Total amount column (if no per-unit price):", self.amount_col)
        form.addRow("Fee column:", self.fee_col)
        form.addRow("Split ratio column (splits only, e.g. 2:1):", self.ratio_col)
        form.addRow("Currency column (e.g. USD; optional):", self.currency_col)
        form.addRow("Exchange rate column (to your base currency; optional):", self.fx_rate_col)
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
            ratio_col=self.ratio_col.text().strip() or None,
            currency_col=self.currency_col.text().strip() or None,
            fx_rate_col=self.fx_rate_col.text().strip() or None,
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
            # Post in chronological order whatever order the file is in.
            ordered_rows = order_rows_for_posting(result.rows)
            total = len(ordered_rows)

            for j, row in enumerate(ordered_rows):
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
