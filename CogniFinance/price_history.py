# price_history.py
# For CogniFinance -- baseline pricing tier for Investments & Crypto.
#
# This is tier 1 of the brief's tiered pricing approach: CSV import of
# price/rate history, zero network, always available. This alone fully
# serves a privacy-purist user -- the app stays 100% functional with no
# network dependency. Tier 2 (the opt-in "Refresh Prices" button) lives
# entirely in price_fetch.py and writes into the same table this module
# defines, via record_price() -- so the holdings/valuation code that
# reads prices never needs to know whether a given price came from a
# CSV a user imported or a network fetch they triggered.
#
# Parsing follows the project's existing OFX/CSV importer naming
# convention (parse_*_to_*, a pure parser separate from any DB/UI
# concern) -- see ofx_import.py's parse_ofx_to_ledger_transactions for
# the pattern this mirrors. NOTE: I didn't have ofx_import.py available
# to match its exact column-sniffing/error conventions, so the parser
# below is a reasonable independent implementation, not a byte-for-byte
# mirror -- worth a quick comparison pass against ofx_import.py's actual
# conventions before this ships.

import csv
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Dict

logger = logging.getLogger(__name__)

# Column-name aliases we'll accept, case-insensitively, so a Stooq-style
# export (date, close) and a hand-built one (Date, Price) both work.
SYMBOL_ALIASES = ("symbol", "ticker", "asset")
DATE_ALIASES = ("date", "datetime")
PRICE_ALIASES = ("price", "close", "rate", "value")


@dataclass
class PricePoint:
    symbol: str
    date: str  # ISO "YYYY-MM-DD"
    price: Decimal


class PriceImportError(ValueError):
    pass


def _find_column(fieldnames: List[str], aliases) -> Optional[str]:
    lower_map = {f.strip().lower(): f for f in fieldnames}
    for alias in aliases:
        if alias in lower_map:
            return lower_map[alias]
    return None


def parse_price_csv_to_price_points(path: str, default_symbol: Optional[str] = None) -> List[PricePoint]:
    """Pure parse: reads a CSV of price history into PricePoint objects.
    No DB access, no side effects -- same spirit as the ledger's own
    pure parsers.

    Two supported shapes:
      1. Multi-symbol: a `symbol`/`ticker` column present -- each row's
         own value is used.
      2. Single-symbol-per-file (e.g. a per-ticker Stooq daily-bar
         export with just date/close columns) -- caller must pass
         `default_symbol`, applied to every row.

    Raises PriceImportError on a structurally unusable file (missing
    date/price columns, or a multi-symbol file with no symbol column
    and no default_symbol given). Skips individual rows with an
    unparseable date or price rather than failing the whole import,
    logging a warning for each -- consistent with wanting a partial,
    honest import over an all-or-nothing one for what's meant to be a
    forgiving baseline path.
    """
    points: List[PricePoint] = []
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                raise PriceImportError(f"'{path}' has no header row.")

            date_col = _find_column(reader.fieldnames, DATE_ALIASES)
            price_col = _find_column(reader.fieldnames, PRICE_ALIASES)
            symbol_col = _find_column(reader.fieldnames, SYMBOL_ALIASES)

            if date_col is None or price_col is None:
                raise PriceImportError(
                    f"'{path}' is missing a recognizable date and/or price column. "
                    f"Found columns: {reader.fieldnames}"
                )
            if symbol_col is None and default_symbol is None:
                raise PriceImportError(
                    f"'{path}' has no symbol/ticker column, and no default_symbol was given. "
                    f"Pass the symbol explicitly for single-symbol files."
                )

            for i, row in enumerate(reader, start=2):  # start=2: header is row 1
                symbol = (row.get(symbol_col) if symbol_col else default_symbol) or ""
                symbol = symbol.strip().upper()
                raw_date = (row.get(date_col) or "").strip()
                raw_price = (row.get(price_col) or "").strip()

                if not symbol or not raw_date or not raw_price:
                    logger.warning(f"price_history: skipping row {i} in '{path}' (missing field).")
                    continue

                date_str = _normalize_date(raw_date)
                if date_str is None:
                    logger.warning(f"price_history: skipping row {i} in '{path}' (unparseable date '{raw_date}').")
                    continue

                try:
                    price = Decimal(raw_price.replace(",", ""))
                except InvalidOperation:
                    logger.warning(f"price_history: skipping row {i} in '{path}' (unparseable price '{raw_price}').")
                    continue

                if price < 0:
                    logger.warning(f"price_history: skipping row {i} in '{path}' (negative price).")
                    continue

                points.append(PricePoint(symbol=symbol, date=date_str, price=price))
    except FileNotFoundError:
        raise PriceImportError(f"File not found: '{path}'")

    return points


def _normalize_date(raw: str) -> Optional[str]:
    """Accepts YYYY-MM-DD, MM/DD/YYYY, or YYYY/MM/DD; returns ISO or
    None if none of those match. Kept intentionally narrow -- widen
    this against whatever ofx_import.py already normalizes to, rather
    than duplicating a broader date-guessing routine independently."""
    raw = raw.strip()
    for fmt_sep, order in ((("-",), "ymd"), (("/",), "ymd"), (("/",), "mdy")):
        sep = fmt_sep[0]
        if sep not in raw:
            continue
        parts = raw.split(sep)
        if len(parts) != 3:
            continue
        try:
            if order == "ymd" and len(parts[0]) == 4:
                y, m, d = parts
            elif order == "mdy" and len(parts[2]) == 4:
                m, d, y = parts
            else:
                continue
            y, m, d = int(y), int(m), int(d)
            return f"{y:04d}-{m:02d}-{d:02d}"
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------
# Storage (shared table; price_fetch.py writes here too)
# ---------------------------------------------------------------

def _init_db(gl):
    conn, cur = gl._connect()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sql_price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                date TEXT NOT NULL,
                price TEXT NOT NULL,
                source TEXT NOT NULL,
                UNIQUE(symbol, date, source)
            )
        """)
        conn.commit()
    finally:
        conn.close()


def record_price(gl, symbol: str, date: str, price: Decimal, source: str) -> None:
    _init_db(gl)
    conn, cur = gl._connect()
    try:
        cur.execute(
            "INSERT OR REPLACE INTO sql_price_history (symbol, date, price, source) VALUES (?, ?, ?, ?)",
            (symbol.strip().upper(), date, str(price), source),
        )
        conn.commit()
    finally:
        conn.close()


def import_csv(gl, path: str, default_symbol: Optional[str] = None, source: str = "csv_import") -> int:
    """Parse + persist in one call for the common case. Returns the
    number of price points stored."""
    points = parse_price_csv_to_price_points(path, default_symbol=default_symbol)
    for p in points:
        record_price(gl, p.symbol, p.date, p.price, source)
    return len(points)


def get_latest_price(gl, symbol: str) -> Optional[Decimal]:
    _init_db(gl)
    conn, cur = gl._connect()
    try:
        cur.execute(
            "SELECT price FROM sql_price_history WHERE symbol = ? ORDER BY date DESC LIMIT 1",
            (symbol.strip().upper(),),
        )
        row = cur.fetchone()
        return Decimal(row[0]) if row else None
    finally:
        conn.close()


def get_price_on(gl, symbol: str, date: str) -> Optional[Decimal]:
    """Latest known price on or before `date` -- useful for valuing a
    holding as-of a report date without needing an exact-day match."""
    _init_db(gl)
    conn, cur = gl._connect()
    try:
        cur.execute(
            "SELECT price FROM sql_price_history WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT 1",
            (symbol.strip().upper(), date),
        )
        row = cur.fetchone()
        return Decimal(row[0]) if row else None
    finally:
        conn.close()


def get_price_history(gl, symbol: str) -> List[Dict]:
    _init_db(gl)
    conn, cur = gl._connect()
    try:
        cur.execute(
            "SELECT date, price, source FROM sql_price_history WHERE symbol = ? ORDER BY date",
            (symbol.strip().upper(),),
        )
        return [{"date": r[0], "price": Decimal(r[1]), "source": r[2]} for r in cur.fetchall()]
    finally:
        conn.close()
