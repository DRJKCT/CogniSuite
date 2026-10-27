#Ledger v1.2
#For CogniFinance ver 0.9.6

import os
import glob
import hashlib
import json
import logging
import re
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import List, Dict, Any, Tuple, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from sqlcipher3 import dbapi2 as sqlite3
from calendar import monthrange

from config import BACKUP_DIR
from period_lock import PeriodLocks, PeriodLockMixin, PeriodLockedError
from merchant_normalization import normalize_merchant
from recurring_engine import (
    detect_recurring_candidates,
    compute_confidence,
    compute_declared_confidence,
    amount_stability,
    INTERVALS,
    PROMOTE_THRESHOLD,
)

# Currency Constants and Helper
ZERO = Decimal("0.00")
CENT = Decimal("0.01")

# --- Merchant Memory / learned categorization tunables ---
# All in one place per design decision: never hardcode these inline.
MERCHANT_MEMORY_VOLUME_CAP = 10          # occurrences at which volume_factor saturates to 1.0
MERCHANT_MEMORY_MIN_OCCURRENCES = 2      # below this, don't surface a suggestion at all
MERCHANT_MEMORY_SUGGEST_THRESHOLD = 0.60 # low bar: show a suggestion, user still reviews it
MERCHANT_MEMORY_PROMOTE_THRESHOLD = 0.95 # high bar: allow "Promote to Rule"
MERCHANT_MEMORY_RATING_MODIFIERS = {"green": 1.2, "yellow": 1.0}

#logging for silent ledger failures
logger = logging.getLogger(__name__)

def money(value) -> Decimal:
    if value is None:
        return ZERO
    return Decimal(str(value)).quantize(
        CENT,
        rounding=ROUND_HALF_UP
    )

PASSPHRASE = os.environ.get("LEDGER_DB_PASSPHRASE")
SQLCIPHER_COMPATIBILITY = int(os.environ.get("LEDGER_SQLCIPHER_COMPATIBILITY", "4"))
# Pinned explicitly rather than relying on whatever cipher_compatibility's
# implicit default happens to be -- that way the brute-force cost of a
# stolen database file doesn't silently change if the SQLCipher library
# is upgraded and its defaults shift. 256,000 matches SQLCipher 4's own
# default as of this writing; override via env var to raise it further.
SQLCIPHER_KDF_ITER = int(os.environ.get("LEDGER_SQLCIPHER_KDF_ITER", "256000"))


@dataclass
class Budget:
    category: str
    month: str
    amount: Decimal


@dataclass
class TransactionLine:
    account_name: str
    dr: Decimal = ZERO
    cr: Decimal = ZERO
    id: Optional[int] = None  # the sql_transaction_lines.id, when known --
                               # None for a line that doesn't exist in the
                               # DB yet (e.g. a brand-new line in an edit,
                               # or any line from record_transaction's
                               # plain 3-tuple input, which never had one
                               # to begin with).


@dataclass
class Transaction:
    id: Optional[int]
    date: str
    description: str
    lines: List[TransactionLine] = field(default_factory=list)
    imported_id: Optional[str] = None
    reconciled: bool = False
    change_reason: Optional[str] = None
    # Carried for display/audit purposes only (e.g. a UI showing a
    # voided transaction struck through, or linking to its reversal).
    # Never used to exclude a transaction from balances -- see
    # _load_from_db(): every transaction counts, including voided
    # ones, since void_transaction() posts an explicit reversal entry
    # rather than removing the original. The ledger is append-only;
    # nothing is ever excluded, only offset.
    is_voided: bool = False
    reversed_by_tx_id: Optional[int] = None
    reverses_tx_id: Optional[int] = None

@dataclass
class CategoryRule:
    id: Optional[int]
    keyword: str
    category: str
    case_sensitive: bool = False
    enabled: bool = True


# Regex to match invalid XML 1.0 characters (control chars, surrogates, etc.)
_ILLEGAL_XML_RE = re.compile(
    r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f'
    r'\ud800-\udfff\ufdd0-\ufdef\ufffe\uffff]'
)

_FORMULA_INJECTION_CHARS = ('+', '-', '=', '@')
_CSV_FORMULA_PREFIXES = ('=', '+', '-', '@', '\t', '\r')

def _sanitize_for_csv(value):
    """
    Neutralize CSV formula injection. When a cell's text begins with
    '=', '+', '-', '@', tab, or CR, Excel/LibreOffice/Google Sheets may
    evaluate it as a formula rather than display it as text. Prefixing
    with a single quote turns the cell into a text literal; the visible
    content is unchanged. Numbers and non-strings pass through.
    """
    if isinstance(value, str):
        if value and value[0] in _CSV_FORMULA_PREFIXES:
            return "'" + value
    return value

def _sanitize_for_excel(value):
    """Strips illegal XML control characters from strings to prevent openpyxl crashes."""
    if isinstance(value, str):
        value = _ILLEGAL_XML_RE.sub('', value)
        if value and value[0] in _FORMULA_INJECTION_CHARS:
            value = "'" + value
    return value


# --- Report Lines (optional CoA presentation grouping) ---
# Accounts may be tagged with a "report line"; the balance sheet and P&L
# renderers group by report line when present, falling back to a flat
# per-account listing otherwise. Each report line implies exactly one
# (account_type, subcategory) pair -- the user never picks the
# subcategory directly, it's a rendering-time derivation.
REPORT_LINE_METADATA = {
    # Assets
    "Cash and Cash Equivalents":     ("Asset", "Current Assets"),
    "Accounts Receivable":           ("Asset", "Current Assets"),
    "Inventory":                     ("Asset", "Current Assets"),
    "Prepaid Expenses":              ("Asset", "Current Assets"),
    "Short-Term Investments":        ("Asset", "Current Assets"),
    "Other Current Assets":          ("Asset", "Current Assets"),
    "Property, Plant & Equipment":   ("Asset", "Non-Current Assets"),
    "Intangible Assets":             ("Asset", "Non-Current Assets"),
    "Long-Term Investments":         ("Asset", "Non-Current Assets"),
    "Other Non-Current Assets":      ("Asset", "Non-Current Assets"),

    # Liabilities
    "Accounts Payable":              ("Liability", "Current Liabilities"),
    "Credit Cards":                  ("Liability", "Current Liabilities"),
    "Accrued Liabilities":           ("Liability", "Current Liabilities"),
    "Short-Term Debt":               ("Liability", "Current Liabilities"),
    "Unearned Revenue":              ("Liability", "Current Liabilities"),
    "Other Current Liabilities":     ("Liability", "Current Liabilities"),
    "Long-Term Debt":                ("Liability", "Non-Current Liabilities"),
    "Lease Obligations":             ("Liability", "Non-Current Liabilities"),
    "Other Non-Current Liabilities": ("Liability", "Non-Current Liabilities"),

    # Equity
    "Owner's Contributions":         ("Equity", "Contributed Capital"),
    "Common Stock":                  ("Equity", "Contributed Capital"),
    "Retained Earnings":             ("Equity", "Earnings"),
    "Current Year Earnings":         ("Equity", "Earnings"),
    "Owner's Draws":                 ("Equity", "Distributions"),
    "Dividends":                     ("Equity", "Distributions"),

    # Revenue
    "Product Sales":                 ("Revenue", "Operating Revenue"),
    "Service Revenue":               ("Revenue", "Operating Revenue"),
    "Other Operating Revenue":       ("Revenue", "Operating Revenue"),
    "Interest Income":               ("Revenue", "Non-Operating Revenue"),
    "Gain on Sale of Assets":        ("Revenue", "Non-Operating Revenue"),
    "Other Income":                  ("Revenue", "Non-Operating Revenue"),

    # Expenses
    "Materials":                     ("Expense", "Cost of Goods Sold"),
    "Direct Labour":                 ("Expense", "Cost of Goods Sold"),
    "Manufacturing Overhead":        ("Expense", "Cost of Goods Sold"),
    "Salaries and Wages":            ("Expense", "Operating Expenses"),
    "Rent":                          ("Expense", "Operating Expenses"),
    "Utilities":                     ("Expense", "Operating Expenses"),
    "Office Supplies":               ("Expense", "Operating Expenses"),
    "Professional Fees":             ("Expense", "Operating Expenses"),
    "Marketing and Advertising":     ("Expense", "Operating Expenses"),
    "Insurance":                     ("Expense", "Operating Expenses"),
    "Travel and Meals":              ("Expense", "Operating Expenses"),
    "Other Operating Expenses":      ("Expense", "Operating Expenses"),
    "Depreciation Expense":          ("Expense", "Depreciation & Amortization"),
    "Amortization Expense":          ("Expense", "Depreciation & Amortization"),
    "Interest Expense":              ("Expense", "Interest & Bank Charges"),
    "Bank Charges":                  ("Expense", "Interest & Bank Charges"),
    "Income Tax Expense":            ("Expense", "Income Tax"),

    # Investments (traditional securities) -- see investment_engine.py
    "Investment — Equities":         ("Asset", "Investments"),
    "Investment — Bonds":            ("Asset", "Investments"),
    "Investment — Mutual Funds":     ("Asset", "Investments"),
    "Investment — Realized Gains":   ("Revenue", "Investment Income"),
    "Investment — Dividend Income":  ("Revenue", "Investment Income"),
    "Investment — Realized Losses":  ("Expense", "Investment Losses"),

    # Crypto -- see crypto_engine.py
    "Crypto — Held":                 ("Asset", "Digital Assets"),
    "Crypto — Staked":               ("Asset", "Digital Assets"),
    "Crypto — LP/Yield":             ("Asset", "Digital Assets"),
    "Crypto — Realized Gains":       ("Revenue", "Crypto Income"),
    "Crypto — Staking Income":       ("Revenue", "Crypto Income"),
    "Crypto — Realized Losses":      ("Expense", "Crypto Losses"),
}

# Precomputed lookups. Built once at import so the account dialog and
# every renderer do O(1) dict lookups rather than re-deriving the
# relationships on each call.
VALID_REPORT_LINES_BY_TYPE: Dict[str, List[str]] = {}
REPORT_LINE_TO_TYPE: Dict[str, str] = {}
REPORT_LINE_TO_SUBCATEGORY: Dict[str, str] = {}
for _rl, (_t, _sub) in REPORT_LINE_METADATA.items():
    VALID_REPORT_LINES_BY_TYPE.setdefault(_t, []).append(_rl)
    REPORT_LINE_TO_TYPE[_rl] = _t
    REPORT_LINE_TO_SUBCATEGORY[_rl] = _sub


class Account:
    NORMAL_BALANCES = {
        "Asset": "DR",
        "Expense": "DR",
        "Liability": "CR",
        "Equity": "CR",
        "Revenue": "CR",
    }

    def __init__(self, name: str, account_type: str, report_line: Optional[str] = None):
        if account_type not in self.NORMAL_BALANCES:
            raise ValueError("Invalid account type.")
        if report_line is not None and REPORT_LINE_TO_TYPE.get(report_line) != account_type:
            raise ValueError(f"'{report_line}' is not a valid report line for account type '{account_type}'.")
        self.name = name
        self.account_type = account_type
        self.report_line = report_line
        self.entries: List[Dict[str, Any]] = []

    @property
    def subcategory(self) -> Optional[str]:
        """Derived presentation grouping -- None when no report_line is set, so
        renderers can tell 'unclassified' apart from a real subcategory
        and fall back to a flat listing rather than a fake bucket label."""
        return REPORT_LINE_TO_SUBCATEGORY.get(self.report_line)

    def add_entry(self, date: str, description: str, dr: Decimal, cr: Decimal):
        self.entries.append({"date": date, "description": description, "dr": money(dr), "cr": money(cr)})

    def get_balance(self) -> Decimal:
        total_dr = sum((e["dr"] for e in self.entries), ZERO)
        total_cr = sum((e["cr"] for e in self.entries), ZERO)
        bal = total_dr - total_cr if self.NORMAL_BALANCES[self.account_type] == "DR" else total_cr - total_dr
        return money(bal)


class GeneralLedger(PeriodLockMixin):
    def __init__(self, db_path: str, dek_hex: str):
        if not dek_hex:
            raise ValueError("A DEK hex string is strictly required to unlock the ledger data")

        self.db_path = db_path
        self.dek_hex = dek_hex
        self.accounts: Dict[str, Account] = {}
        self.transactions: List[Transaction] = []
        self.category_rules: List[CategoryRule] = []
        self.period_locks = PeriodLocks()

        # Active connection/pending transactions used only for atomic sync
        # batches. Normal ledger operations continue to use short-lived
        # connections as before.
        self._active_transaction_conn = None
        self._active_transaction_pending: List[Transaction] = []

        self._init_db()
        self._load_from_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        try:
            cur.execute(f"PRAGMA key = \"x'{self.dek_hex}'\"")
            cur.execute(f"PRAGMA cipher_compatibility = {SQLCIPHER_COMPATIBILITY}")
            cur.execute(f"PRAGMA kdf_iter = {SQLCIPHER_KDF_ITER}")

            # --- NEW: Wait up to 5 seconds if the DB is temporarily locked ---
            cur.execute("PRAGMA busy_timeout = 5000;")
            cur.execute("SELECT count(*) FROM sqlite_master;")
        except sqlite3.DatabaseError as e:
            conn.close()
            raise ValueError("Failed to unlock database. Invalid passphrase or corrupted file.") from e
        return conn, cur

    def _harden_file_permissions(self, path: str) -> None:
        """Best-effort: restrict a sensitive file to owner read/write only."""
        try:
            if os.path.exists(path):
                os.chmod(path, 0o600)
        except OSError:
            pass

    def _init_db(self):
        conn, cur = self._connect()
        try:
            # --- NEW: Enable WAL mode so readers (audit.py) don't block writers (ledger.py) ---
            cur.execute("PRAGMA journal_mode = WAL;")

            cur.execute("CREATE TABLE IF NOT EXISTS sql_accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, account_type TEXT, report_line TEXT)")

            cur.execute("CREATE TABLE IF NOT EXISTS sql_transactions ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "date TEXT, description TEXT, imported_id TEXT, "
                    "reconciled INTEGER DEFAULT 0, "
                    "is_voided INTEGER DEFAULT 0, "
                    "reversed_by_tx_id INTEGER, "
                    "reverses_tx_id INTEGER, "
                    "change_reason TEXT)")

            cur.execute("CREATE TABLE IF NOT EXISTS sql_transaction_lines (id INTEGER PRIMARY KEY AUTOINCREMENT, tx_id INTEGER, account_name TEXT, dr TEXT, cr TEXT, FOREIGN KEY(tx_id) REFERENCES sql_transactions(id))")
            cur.execute("CREATE TABLE IF NOT EXISTS sql_category_rules (id INTEGER PRIMARY KEY AUTOINCREMENT, keyword TEXT, category TEXT, case_sensitive INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1)")

            # ===================================================================
            # BUDGET ENGINE (ZBB + Envelope)
            # ===================================================================
            cur.execute(
                "CREATE TABLE IF NOT EXISTS sql_budget_envelopes ("
                "category_name      TEXT PRIMARY KEY, "
                "linked_coa_account TEXT, "
                "rollover_policy    TEXT NOT NULL DEFAULT 'carry_forward', "
                "is_active          INTEGER NOT NULL DEFAULT 1)"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS sql_budget_income_plan ("
                "month           TEXT PRIMARY KEY, "
                "expected_amount TEXT NOT NULL, "
                "income_source   TEXT NOT NULL DEFAULT 'last_month', "
                "derived_amount  TEXT, "
                "created_at      TEXT NOT NULL, "
                "updated_at      TEXT NOT NULL)"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS sql_budget_ledger ("
                "id            INTEGER PRIMARY KEY AUTOINCREMENT, "
                "month         TEXT NOT NULL, "
                "category_name TEXT NOT NULL, "
                "event_type    TEXT NOT NULL, "
                "amount        TEXT NOT NULL, "
                "related_id    INTEGER, "
                "note          TEXT, "
                "created_at    TEXT NOT NULL)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_budget_ledger_category_month "
                "ON sql_budget_ledger (category_name, month)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_budget_ledger_month "
                "ON sql_budget_ledger (month)"
            )
            # Period locks: one row per account = the date it is locked
            # through (inclusive). The log is append-only so every lock and
            # every reopen (a control override) leaves a trail.
            cur.execute(
                "CREATE TABLE IF NOT EXISTS sql_period_locks ("
                "account_name TEXT PRIMARY KEY, "
                "locked_through TEXT NOT NULL, "
                "updated_at TEXT)"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS sql_period_lock_log ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "account_name TEXT NOT NULL, "
                "action TEXT NOT NULL, "
                "previous_through TEXT, "
                "new_through TEXT, "
                "logged_at TEXT)"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS sql_statement_balances ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "account_name TEXT, "
                "date TEXT, "
                "balance TEXT, "
                "source TEXT, "
                "recorded_at TEXT)"
            )

            # --- Merchant Memory / learned categorization ---
            # Draft rows are staging only: written when a merchant is first
            # normalized, promoted (copied into sql_merchant_pattern, then
            # deleted) on confirm/reconcile, and simply orphaned -- never
            # cleaned up here -- if the transaction is voided/edited first.
            cur.execute(
                "CREATE TABLE IF NOT EXISTS sql_merchant_draft ("
                "tx_id INTEGER NOT NULL, "
                "line_id INTEGER NOT NULL, "
                "raw_string TEXT, "
                "merchant_key TEXT, "
                "merchant_display TEXT, "
                "category_suggested TEXT, "
                "created_at TEXT, "
                "PRIMARY KEY (tx_id, line_id))"
            )

            # One row per (merchant_key, category). count_total for the
            # confidence formula is deliberately NOT a column here -- it's
            # SUM(occurrences) across all rows sharing a merchant_key,
            # computed at query time so it can never drift from the raw
            # per-category counts. rating is 'green' or 'yellow' only --
            # 'red' never gets stored: it deletes the row and writes to
            # sql_merchant_blacklist instead (see reject_pattern()).
            cur.execute(
                "CREATE TABLE IF NOT EXISTS sql_merchant_pattern ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "merchant_key TEXT NOT NULL, "
                "merchant_display TEXT NOT NULL, "
                "category TEXT NOT NULL, "
                "occurrences INTEGER NOT NULL DEFAULT 0, "
                "rating TEXT NOT NULL DEFAULT 'yellow', "
                "first_seen_at TEXT, "
                "last_seen_at TEXT, "
                "promoted_rule_id INTEGER, "
                "UNIQUE(merchant_key, category))"
            )

            # Keyed on (merchant_key, category), never merchant alone --
            # blacklisting one category for a merchant must never suppress
            # a different category for that same merchant.
            cur.execute(
                "CREATE TABLE IF NOT EXISTS sql_merchant_blacklist ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "merchant_key TEXT NOT NULL, "
                "category TEXT NOT NULL, "
                "blacklisted_at TEXT, "
                "UNIQUE(merchant_key, category))"
            )

            # --- Recurring Charges ---
            # Deliberately separate from the Merchant Memory tables
            # above, and deliberately shaped like them. See the
            # "Recurring Charges" section in GeneralLedger for why
            # they cannot be shared: sql_merchant_pattern is keyed
            # UNIQUE(merchant_key, category) and its confidence
            # denominator is SUM(occurrences) per merchant_key, so
            # folding recurring rows into it would both lose multiple
            # cadences for one merchant AND silently corrupt the
            # merchant-memory confidence score.
            #
            # Staged hypotheses, awaiting a human. One row per
            # (merchant, category, interval).
            cur.execute(
                "CREATE TABLE IF NOT EXISTS sql_recurring_draft ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "merchant_key TEXT NOT NULL, "
                "merchant_display TEXT NOT NULL, "
                "category TEXT NOT NULL, "
                "interval TEXT NOT NULL, "
                "expected_amount TEXT NOT NULL, "
                "amount_variance TEXT NOT NULL, "
                "expected_next_date TEXT, "
                "occurrences INTEGER NOT NULL DEFAULT 0, "
                "interval_regularity REAL NOT NULL DEFAULT 0.0, "
                "evidence_dates TEXT, "
                "created_at TEXT, "
                "UNIQUE(merchant_key, category, interval))"
            )

            # Confirmed patterns -- the record consumers read. rating is
            # 'green' or 'yellow' only; 'red' never gets stored (it
            # deletes the row and writes to sql_recurring_blacklist
            # instead, see reject_recurring).
            cur.execute(
                "CREATE TABLE IF NOT EXISTS sql_recurring_pattern ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "merchant_key TEXT NOT NULL, "
                "merchant_display TEXT NOT NULL, "
                "category TEXT NOT NULL, "
                "interval TEXT NOT NULL, "
                "expected_amount TEXT NOT NULL, "
                "amount_variance TEXT NOT NULL, "
                "expected_next_date TEXT, "
                "occurrences INTEGER NOT NULL DEFAULT 0, "
                "interval_regularity REAL NOT NULL DEFAULT 0.0, "
                "rating TEXT NOT NULL DEFAULT 'yellow', "
                "user_declared INTEGER NOT NULL DEFAULT 0, "
                "source TEXT NOT NULL DEFAULT 'auto_detected', "
                "first_seen_at TEXT, "
                "last_seen_at TEXT, "
                "created_at TEXT, "
                "updated_at TEXT, "
                "UNIQUE(merchant_key, category, interval))"
            )

            # False-positive suppression, keyed on the same triple as
            # the pattern table so dismissing one cadence for a
            # merchant never suppresses a different cadence for that
            # same merchant. Always individually reversible.
            cur.execute(
                "CREATE TABLE IF NOT EXISTS sql_recurring_blacklist ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "merchant_key TEXT NOT NULL, "
                "category TEXT NOT NULL, "
                "interval TEXT NOT NULL, "
                "blacklisted_at TEXT, "
                "UNIQUE(merchant_key, category, interval))"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS sql_transaction_history ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "tx_id INTEGER, "
                "change_type TEXT, "
                "changed_at TEXT, "
                "date TEXT, "
                "description TEXT, "
                "lines_json TEXT, "
                "change_reason TEXT, "
                "prev_hash TEXT, "   # NEW: Hash of the previous row
                "row_hash TEXT)"     # NEW: Hash of this row
            )

            cur.execute(
                "CREATE TABLE IF NOT EXISTS tax_profile ("
                "section_name TEXT PRIMARY KEY, "
                "data_json TEXT NOT NULL, "
                "updated_at TEXT)"
            )
            cur.execute("PRAGMA table_info(sql_transactions)")
            columns = [info[1] for info in cur.fetchall()]

            if "is_voided" not in columns:
                cur.execute("ALTER TABLE sql_transactions ADD COLUMN is_voided INTEGER DEFAULT 0")
            if "reversed_by_tx_id" not in columns:
                cur.execute("ALTER TABLE sql_transactions ADD COLUMN reversed_by_tx_id INTEGER")
            if "reverses_tx_id" not in columns:
                cur.execute("ALTER TABLE sql_transactions ADD COLUMN reverses_tx_id INTEGER")
            if "change_reason" not in columns:
                cur.execute("ALTER TABLE sql_transactions ADD COLUMN change_reason TEXT")

            # Column additions for the recurring tables, for databases
            # created before they existed. Same IF NOT EXISTS + ALTER
            # idiom as sql_transactions above.
            for _table, _columns in (
                ("sql_recurring_draft", {"evidence_dates": "TEXT"}),
                ("sql_recurring_pattern", {
                    "interval_regularity": "REAL NOT NULL DEFAULT 0.0",
                    "user_declared": "INTEGER NOT NULL DEFAULT 0",
                    "source": "TEXT NOT NULL DEFAULT 'auto_detected'",
                    "updated_at": "TEXT",
                }),
            ):
                cur.execute(f"PRAGMA table_info({_table})")
                _existing = {info[1] for info in cur.fetchall()}
                for _col, _ddl in _columns.items():
                    if _col not in _existing:
                        cur.execute(f"ALTER TABLE {_table} ADD COLUMN {_col} {_ddl}")

            conn.commit()
        finally:
            conn.close()
        self._harden_file_permissions(self.db_path)

    def _load_from_db(self):
        self.accounts = {}
        self.transactions = []
        self.category_rules = []

        conn, cur = self._connect()
        try:
            cur.execute("SELECT name, account_type, report_line FROM sql_accounts")
            rows = cur.fetchall()

            if not rows:
                seeds = [
                    ("Checking Account",      "Asset",     "Cash and Cash Equivalents"),
                    ("Salary Income",         "Revenue",   "Other Operating Revenue"),
                    ("Groceries",             "Expense",   "Other Operating Expenses"),
                    ("Uncategorized Expense", "Expense",   None),
                    ("Uncategorized Income",  "Revenue",   None),
                    ("Credit Card",           "Liability", "Credit Cards"),
                ]
                for name, acc_type, report_line in seeds:
                    cur.execute(
                        "INSERT INTO sql_accounts (name, account_type, report_line) VALUES (?, ?, ?)",
                        (name, acc_type, report_line),
                    )
                conn.commit()
                cur.execute("SELECT name, account_type, report_line FROM sql_accounts")
                rows = cur.fetchall()

            for name, acc_type, report_line in rows:
                self.accounts[name] = Account(name, acc_type, report_line)

            # Load EVERY transaction, voided or not -- the ledger is
            # append-only, and void_transaction() corrects a mistake by
            # posting an explicit reversal entry, not by making the
            # original disappear. Excluding is_voided=1 rows here would
            # mean only the reversal counts, leaving every voided
            # transaction's accounts off by double the original amount
            # (the bug this fixes). is_voided/reversed_by_tx_id/
            # reverses_tx_id are still carried on the Transaction object
            # for a UI to display a voided entry distinctly if it wants
            # to -- they just no longer gate which rows count.
            cur.execute(
                "SELECT id, date, description, imported_id, reconciled, change_reason, "
                "is_voided, reversed_by_tx_id, reverses_tx_id FROM sql_transactions ORDER BY date"
            )
            for tx_id, date, desc, imp_id, rec, change_reason, is_voided, reversed_by, reverses in cur.fetchall():
                cur.execute("SELECT id, account_name, dr, cr FROM sql_transaction_lines WHERE tx_id = ?", (tx_id,))
                lines = [TransactionLine(acc_name, money(dr) if dr else ZERO, money(cr) if cr else ZERO, id=line_id) for line_id, acc_name, dr, cr in cur.fetchall()]
                tx = Transaction(tx_id, date, desc, lines, imp_id, bool(rec), change_reason,
                                  is_voided=bool(is_voided), reversed_by_tx_id=reversed_by, reverses_tx_id=reverses)
                self._apply_transaction_to_accounts(tx)
                self.transactions.append(tx)

            cur.execute("SELECT id, keyword, category, case_sensitive, enabled FROM sql_category_rules ORDER BY id")
            for rid, kw, cat, cs, en in cur.fetchall():
                self.category_rules.append(CategoryRule(rid, kw, cat, bool(cs), bool(en)))

            cur.execute("SELECT account_name, locked_through FROM sql_period_locks")
            self.period_locks = PeriodLocks(dict(cur.fetchall()))
        finally:
            conn.close()

    def _imported_id_exists(self, imported_id):
        """Return True if imported_id already exists in the ledger.

        During an atomic sync batch, query the active transaction connection
        so IDs inserted earlier in the same uncommitted batch are visible.
        For normal operations, _connect() returns (conn, cur), so both values
        must be unpacked before executing the query.
        """
        if not imported_id:
            return False

        active_conn = getattr(self, "_active_transaction_conn", None)
        if active_conn is not None:
            row = active_conn.execute(
                "SELECT 1 FROM sql_transactions WHERE imported_id = ? LIMIT 1",
                (imported_id,)
            ).fetchone()
            return row is not None

        conn, cur = self._connect()
        try:
            cur.execute(
                "SELECT 1 FROM sql_transactions WHERE imported_id = ? AND is_voided = 0 LIMIT 1",
                (imported_id,)
            )
            return cur.fetchone() is not None
        finally:
            conn.close()

    def create_hot_backup(self, backup_dir: str, keep_last: int = 5) -> Tuple[bool, str]:
        """
        Creates a safe, point-in-time backup of the encrypted database
        using SQLCipher's VACUUM INTO. Safe to run while the app is active.
        """

        os.makedirs(backup_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(backup_dir, f"ledger_backup_{timestamp}.db")

        conn, cur = self._connect()
        try:

            cur.execute("VACUUM INTO ?", (backup_path,))
            conn.commit()

            # Prune old backups to prevent the folder from consuming all disk space
            files = glob.glob(os.path.join(backup_dir, "ledger_backup_*.db"))
            files.sort(key=os.path.getctime, reverse=True)
            for old_file in files[keep_last:]:
                try:
                    os.remove(old_file)
                except OSError:
                    pass

            return True, backup_path
        except Exception as e:
            logger.error(f"Hot backup failed: {e}")
            return False, str(e)
        finally:
            if conn is not self._main_conn:
                conn.close()


    def begin_transaction(self) -> None:
        """Begin an atomic database transaction for a sync batch."""
        if self._active_transaction_conn is not None:
            raise RuntimeError("A ledger transaction is already active.")

        conn, cur = self._connect()
        try:
            cur.execute("BEGIN")
        except Exception:
            conn.close()
            raise

        self._active_transaction_conn = conn
        self._active_transaction_pending = []

    def commit_transaction(self) -> None:
        """Commit the active batch and then publish its in-memory state."""
        conn = self._active_transaction_conn
        if conn is None:
            raise RuntimeError("No ledger transaction is active.")

        pending = self._active_transaction_pending
        try:
            conn.commit()
            for tx in pending:
                self._apply_transaction_to_accounts(tx)
                self.transactions.append(tx)
        except Exception:
            try:
                conn.rollback()
            finally:
                self._active_transaction_conn = None
                self._active_transaction_pending = []
                conn.close()
            raise

        self._active_transaction_conn = None
        self._active_transaction_pending = []
        conn.close()

    def rollback_transaction(self) -> None:
        """Roll back the active batch and discard all pending state."""
        conn = self._active_transaction_conn
        if conn is None:
            return

        try:
            conn.rollback()
        finally:
            self._active_transaction_conn = None
            self._active_transaction_pending = []
            conn.close()

    def _apply_transaction_to_accounts(self, tx: Transaction):
        for line in tx.lines:
            self.accounts[line.account_name].add_entry(tx.date, tx.description, line.dr, line.cr)

    def create_account(self, name: str, acc_type: str, report_line: Optional[str] = None) -> Tuple[bool, str]:
        if name in self.accounts:
            return False, "Account already exists."
        try:
            new_account = Account(name, acc_type, report_line)  # validates acc_type and report_line together
        except ValueError as e:
            return False, str(e)
        conn, cur = self._connect()
        try:
            cur.execute(
                "INSERT INTO sql_accounts (name, account_type, report_line) VALUES (?, ?, ?)",
                (name, acc_type, report_line),
            )
            conn.commit()
            self.accounts[name] = new_account
            return True, f"Account '{name}' created."
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()

    def delete_account(self, name: str) -> Tuple[bool, str]:
        """Delete an account, but only if it has no transaction history."""
        if name not in self.accounts:
            return False, "Account does not exist."

        acc = self.accounts[name]
        if acc.entries:
            return False, "Cannot delete an account that already has transactions."

        conn, cur = self._connect()
        try:
            cur.execute("DELETE FROM sql_accounts WHERE name = ?", (name,))
            cur.execute("DELETE FROM sql_period_locks WHERE account_name = ?", (name,))
            # Unlink, don't delete -- the envelope and its budget_ledger
            # history are interpretation-layer records independent of
            # whether a GL account still backs them.
            cur.execute("UPDATE sql_budget_envelopes SET linked_coa_account = NULL WHERE linked_coa_account = ?", (name,))
            conn.commit()
            del self.accounts[name]
            self.period_locks.set(name, None)
            return True, f"Account '{name}' deleted."
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()

    def get_valid_report_lines(self, account_type: str) -> List[str]:
        """Report lines valid for a given account_type -- for populating a dropdown."""
        return list(VALID_REPORT_LINES_BY_TYPE.get(account_type, []))

    def set_account_report_line(self, name: str, report_line: Optional[str]) -> Tuple[bool, str]:
        """
        Set or clear an account's report_line. Separate from
        update_account (which handles name/type) since this is a
        narrower, more frequent edit -- classifying an account doesn't
        require touching its name or type.
        """
        if name not in self.accounts:
            return False, "Account does not exist."
        acc = self.accounts[name]
        if report_line is not None and REPORT_LINE_TO_TYPE.get(report_line) != acc.account_type:
            return False, f"'{report_line}' is not a valid report line for account type '{acc.account_type}'."
        conn, cur = self._connect()
        try:
            cur.execute("UPDATE sql_accounts SET report_line = ? WHERE name = ?", (report_line, name))
            conn.commit()
            acc.report_line = report_line
            return True, "Report line updated."
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()

    def get_account_hierarchy(self, name: str) -> Optional[Dict[str, Optional[str]]]:
        """The account_type / subcategory / report_line breakdown for one account."""
        acc = self.accounts.get(name)
        if not acc:
            return None
        return {"account_type": acc.account_type, "subcategory": acc.subcategory, "report_line": acc.report_line}

    def get_accounts_grouped_for_report(self) -> Dict[str, Dict[Optional[str], List[str]]]:
        """
        Every account nested account_type -> subcategory -> [account
        names], sorted at each level. Accounts with no report_line set
        land under the `None` key rather than a fake "Uncategorized"
        label -- the renderer decides whether that means a flat listing,
        a literal "Uncategorized" heading, or something else; this layer
        just tells you which accounts have a real subcategory and which
        don't.
        """
        grouped: Dict[str, Dict[Optional[str], Dict[Optional[str], List[str]]]] = {}
        for acc in self.accounts.values():
            subcat = acc.subcategory                       # None if no report_line
            rl = acc.report_line                           # None if no report_line
            by_sub = grouped.setdefault(acc.account_type, {})
            by_rl = by_sub.setdefault(subcat, {})
            by_rl.setdefault(rl, []).append(acc.name)

        # Sort account names at the leaf. Sorting the dict keys is left
        # to the renderer, because the correct display order (Assets /
        # Liabilities / Equity, and Unclassified-last within a section)
        # is a presentation concern, not a data concern.
        for by_sub in grouped.values():
            for by_rl in by_sub.values():
                for names in by_rl.values():
                    names.sort()

        return grouped

    def update_account(self, old_name: str, new_name: str, new_type: str) -> Tuple[bool, str]:
        """Renames and/or changes the type of an account, cascading updates to history, budgets, and rules."""
        if old_name not in self.accounts:
            return False, "Account does not exist."
        if not new_name.strip():
            return False, "Account name cannot be empty."
        if new_name != old_name and new_name in self.accounts:
            return False, "An account with that name already exists."

        acc = self.accounts[old_name]
        name_changed = new_name != old_name
        type_changed = new_type != acc.account_type

        if not name_changed and not type_changed:
            return True, "No changes made."

        conn, cur = self._connect()
        try:
            if name_changed:
                # Cascade the rename across all related tables
                cur.execute("UPDATE sql_accounts SET name = ? WHERE name = ?", (new_name, old_name))
                cur.execute("UPDATE sql_transaction_lines SET account_name = ? WHERE account_name = ?", (new_name, old_name))
                cur.execute("UPDATE sql_period_locks SET account_name = ? WHERE account_name = ?", (new_name, old_name))
                cur.execute("UPDATE sql_period_lock_log SET account_name = ? WHERE account_name = ?", (new_name, old_name))
                # Envelope identity follows the account it tracks, same as
                # category_rules.category below -- rename cascades to the
                # envelope's own category_name (its primary key) AND to
                # linked_coa_account, plus every historical ledger row
                # filed under the old name, so nothing silently orphans
                # under a stale label after a rename.
                cur.execute("UPDATE sql_budget_envelopes SET category_name = ? WHERE category_name = ?", (new_name, old_name))
                cur.execute("UPDATE sql_budget_envelopes SET linked_coa_account = ? WHERE linked_coa_account = ?", (new_name, old_name))
                cur.execute("UPDATE sql_budget_ledger SET category_name = ? WHERE category_name = ?", (new_name, old_name))
                cur.execute("UPDATE sql_category_rules SET category = ? WHERE category = ?", (new_name, old_name))

            if type_changed:
                target_name = new_name if name_changed else old_name
                cur.execute("UPDATE sql_accounts SET account_type = ? WHERE name = ?", (new_type, target_name))
                # A report_line only means something under the type it was
                # picked for ("Accounts Receivable" doesn't exist under
                # Expense) -- clear it rather than leave a stale tier-3
                # label that no longer resolves to a real tier-2 bucket.
                report_line_cleared = acc.report_line is not None and REPORT_LINE_TO_TYPE.get(acc.report_line) != new_type
                if report_line_cleared:
                    cur.execute("UPDATE sql_accounts SET report_line = NULL WHERE name = ?", (target_name,))

            conn.commit()

            # Update in-memory state
            if name_changed:
                acc = self.accounts.pop(old_name)
                self.accounts[new_name] = acc
                acc.name = new_name
                self.period_locks.rename(old_name, new_name)

                # Update rules in memory
                for rule in self.category_rules:
                    if rule.category == old_name:
                        rule.category = new_name

            if type_changed:
                acc.account_type = new_type
                if report_line_cleared:
                    acc.report_line = None

            msg = "Account updated."
            if type_changed and report_line_cleared:
                msg += " Its report line was cleared since it no longer applies under the new type."
            return True, msg
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()

    def record_transaction(self, date: str, description: str, lines_data: List[Tuple[str, Any, Any]], imported_id: Optional[str] = None, persist: bool = True) -> Tuple[bool, str]:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            msg = "Invalid date format. Use YYYY-MM-DD."
            logger.warning(f"Ledger rejected transaction '{description}': {msg}")
            return False, msg

        if len(lines_data) < 2:
            msg = "A transaction must contain at least two lines."
            logger.warning(f"Ledger rejected transaction '{description}': {msg}")
            return False, msg

        try:
            lines = [TransactionLine(acc, money(dr), money(cr)) for acc, dr, cr in lines_data]
            for line in lines:
                if not line.dr.is_finite() or not line.cr.is_finite():
                    raise ValueError(f"non-finite amount on account '{line.account_name}'")
        except (InvalidOperation, ValueError, TypeError) as e:
            # Previously uncaught here -- a garbage amount string raised
            # InvalidOperation at parse time, but NaN/Infinity are
            # different: they parse into a valid Decimal successfully and
            # only blow up LATER on the first comparison (line.dr < ZERO
            # below), which is a much nastier failure mode since the
            # traceback points at an unrelated line, not the bad input.
            # The explicit is_finite() check above catches both cases
            # here, in one place, before either can happen.
            msg = f"Invalid amount: {e}"
            logger.warning(f"Ledger rejected transaction '{description}': {msg}")
            return False, msg

        for line in lines:
            if line.dr < ZERO or line.cr < ZERO:
                msg = "Negative debits or credits are not allowed."
                logger.warning(f"Ledger rejected transaction '{description}': {msg}")
                return False, msg
            if line.dr > ZERO and line.cr > ZERO:
                msg = "A transaction line cannot contain both a debit and a credit."
                logger.warning(f"Ledger rejected transaction '{description}': {msg}")
                return False, msg
            if line.dr == ZERO and line.cr == ZERO:
                msg = "A transaction line must contain either a debit or a credit."
                logger.warning(f"Ledger rejected transaction '{description}': {msg}")
                return False, msg
            if line.account_name not in self.accounts:
                msg = f"Missing account: {line.account_name}"
                logger.warning(f"Ledger rejected transaction '{description}': {msg}")
                return False, msg

        total_dr = sum((l.dr for l in lines), ZERO)
        total_cr = sum((l.cr for l in lines), ZERO)

        if total_dr != total_cr:
            msg = f"Transaction out of balance (Dr: {total_dr}, Cr: {total_cr})."
            logger.warning(f"Ledger rejected transaction '{description}': {msg}")
            return False, msg

        lock_msg = self.period_lock_violation(date, [l.account_name for l in lines])
        if lock_msg:
            logger.warning(f"Ledger rejected transaction '{description}': {lock_msg}")
            return False, lock_msg

        if imported_id and self._imported_id_exists(imported_id):
            msg = f"Duplicate imported transaction ID: {imported_id}"
            logger.warning(f"Ledger rejected transaction '{description}': {msg}")
            return False, msg

        tx = Transaction(None, date, description, lines, imported_id, False)

        if persist:
            try:
                self._persist_transaction(tx)
            except Exception as e:
                logger.error(f"Ledger failed to persist transaction '{description}': {e}")
                return False, str(e)

        if self._active_transaction_conn is not None:
            # Do not expose the transaction through in-memory ledger state
            # until the whole batch has committed successfully.
            self._active_transaction_pending.append(tx)
        else:
            self._apply_transaction_to_accounts(tx)
            self.transactions.append(tx)

        return True, "Posted successfully."

    @staticmethod
    def _lines_to_json(lines) -> str:
        """
        Serialize transaction lines (list of TransactionLine, or of
        (line_id, account_name, dr, cr) tuples) into a JSON string for
        storage in the history table. line_id is included so history
        entries can document WHICH specific line changed in a
        multi-line/split transaction, not just that the transaction
        changed somehow.
        """
        payload = []
        for line in lines:
            if isinstance(line, TransactionLine):
                payload.append({"id": line.id, "account_name": line.account_name, "dr": str(line.dr), "cr": str(line.cr)})
            else:
                line_id, acc, dr, cr = line
                payload.append({"id": line_id, "account_name": acc, "dr": str(money(dr)), "cr": str(money(cr))})
        return json.dumps(payload)

    def _log_history(self, cur, tx_id: Optional[int], change_type: str, date: str, description: str, lines, change_reason: Optional[str] = None) -> None:
        """
        Record a snapshot of a transaction's state into the history table.
        Must be called using the same cursor/connection as the change
        itself so the log is committed atomically with the change it
        describes.

        change_type: "created" | "updated" | "deleted"
        For "created", the snapshot is the transaction as first entered.
        For "updated"/"deleted", the snapshot should be the state
        *before* the change was applied, so history reads as a log of
        prior versions.
        """
        cur.execute("SELECT row_hash FROM sql_transaction_history ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        prev_hash = row[0] if row else "0" * 64  # Genesis block hash if empty

        # 2. Prepare data
        lines_json = self._lines_to_json(lines)
        changed_at = datetime.now().isoformat(timespec="seconds")

        # 3. Compute the cryptographic hash for this specific row
        payload = f"{tx_id}{change_type}{changed_at}{date}{description}{lines_json}{change_reason or ''}{prev_hash}"
        row_hash = hashlib.sha256(payload.encode('utf-8')).hexdigest()

        # 4. Insert with the hashes
        cur.execute(
            "INSERT INTO sql_transaction_history (tx_id, change_type, changed_at, date, description, lines_json, change_reason, prev_hash, row_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tx_id, change_type, changed_at, date, description, lines_json, change_reason, prev_hash, row_hash),
        )

    def _fetch_transaction_snapshot(self, cur, tx_id: int):
        """Read a transaction's current (date, description, lines) directly
        from the DB. lines is a list of (line_id, account_name, dr, cr)."""
        cur.execute("SELECT date, description FROM sql_transactions WHERE id = ?", (tx_id,))
        row = cur.fetchone()
        if row is None:
            return None
        date, description = row
        cur.execute("SELECT id, account_name, dr, cr FROM sql_transaction_lines WHERE tx_id = ?", (tx_id,))
        lines = [(line_id, acc, money(dr) if dr else ZERO, money(cr) if cr else ZERO) for line_id, acc, dr, cr in cur.fetchall()]
        return date, description, lines

    def _persist_transaction(self, tx: Transaction):
        # During an atomic sync batch, reuse the batch connection and defer
        # the commit until commit_transaction(). Otherwise preserve the
        # normal one-transaction/one-connection behavior.
        active_conn = self._active_transaction_conn
        if active_conn is not None:
            cur = active_conn.cursor()
            cur.execute(
                "INSERT INTO sql_transactions (date, description, imported_id, reconciled) VALUES (?, ?, ?, ?)",
                (tx.date, tx.description, tx.imported_id, 1 if tx.reconciled else 0),
            )
            tx_id = cur.lastrowid
            tx.id = tx_id
            for line in tx.lines:
                cur.execute(
                    "INSERT INTO sql_transaction_lines (tx_id, account_name, dr, cr) VALUES (?, ?, ?, ?)",
                    (tx_id, line.account_name, str(line.dr), str(line.cr)),
                )
                line.id = cur.lastrowid
            self._log_history(cur, tx_id, "created", tx.date, tx.description, tx.lines)
            return

        conn, cur = self._connect()
        try:
            cur.execute(
                "INSERT INTO sql_transactions (date, description, imported_id, reconciled) VALUES (?, ?, ?, ?)",
                (tx.date, tx.description, tx.imported_id, 1 if tx.reconciled else 0),
            )
            tx_id = cur.lastrowid
            tx.id = tx_id
            for line in tx.lines:
                cur.execute(
                    "INSERT INTO sql_transaction_lines (tx_id, account_name, dr, cr) VALUES (?, ?, ?, ?)",
                    (tx_id, line.account_name, str(line.dr), str(line.cr)),
                )
                line.id = cur.lastrowid
            self._log_history(cur, tx_id, "created", tx.date, tx.description, tx.lines)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_transactions(self, start_date: Optional[str] = None, end_date: Optional[str] = None, account: Optional[str] = None, search: Optional[str] = None) -> List[Transaction]:
        result = self.transactions
        if start_date:
            try:
                datetime.strptime(start_date, "%Y-%m-%d")
            except ValueError:
                raise ValueError("Invalid start_date format (use YYYY-MM-DD).")
            result = [t for t in result if t.date >= start_date]
        if end_date:
            try:
                datetime.strptime(end_date, "%Y-%m-%d")
            except ValueError:
                raise ValueError("Invalid end_date format (use YYYY-MM-DD).")
            result = [t for t in result if t.date <= end_date]
        if account:
            result = [t for t in result if any(line.account_name == account for line in t.lines)]
        if search:
            q = search.lower()
            result = [t for t in result if q in t.description.lower() or any(q in line.account_name.lower() for line in t.lines)]
        return result

    def get_all_budgets(self) -> List[Budget]:
        """
        Net assigned amount per (category, month), aggregated from
        sql_budget_ledger: assign - unassign + move_in - move_out.
        Summed in Python with Decimal, not SQL, so this can't accumulate
        the binary-float rounding risk a SQL SUM(CAST(... AS REAL)) would
        carry -- consistent with how every other money figure in this
        file is handled.
        """
        conn, cur = self._connect()
        try:
            cur.execute("SELECT category_name, month, event_type, amount FROM sql_budget_ledger")
            rows = cur.fetchall()
        finally:
            conn.close()

        net: Dict[Tuple[str, str], Decimal] = {}
        sign = {"assign": 1, "move_in": 1, "unassign": -1, "move_out": -1}
        for category_name, month, event_type, amount in rows:
            key = (category_name, month)
            net[key] = net.get(key, ZERO) + sign.get(event_type, 0) * money(amount)

        return [Budget(cat, month, amt) for (cat, month), amt in net.items() if amt != ZERO]

    def get_total_revenue_for_month(self, month: str) -> Decimal:
        """Sum of (Revenue credits − Revenue debits) across all transactions dated 'YYYY-MM'."""
        total = ZERO
        for tx in self.transactions:
            if not tx.date.startswith(month):
                continue
            for line in tx.lines:
                acc = self.accounts.get(line.account_name)
                if acc and acc.account_type == "Revenue":
                    total += (line.cr - line.dr)
        return money(total)

    def get_revenue_for_months(self, months: List[str]) -> Decimal:
        """Sum of get_total_revenue_for_month() across months."""
        total = ZERO
        for m in months:
            total += self.get_total_revenue_for_month(m)
        return money(total)

    def get_category_actuals(self, linked_coa_account: str, month: str) -> Decimal:
        """Net spending on linked_coa_account in a single month: debits minus credits.

        Crediting an expense account lowers spending. That is what a refund
        does, and also what the reversal entry posted by void_transaction()
        and edit_transaction() does, so a voided expense nets to zero and an
        edited one counts once. Revenue has always been summed the same
        way (get_total_revenue_for_month: credits minus debits).
        """
        if not linked_coa_account or linked_coa_account not in self.accounts:
            return ZERO
        if self.accounts[linked_coa_account].account_type != "Expense":
            return ZERO
        total = ZERO
        for tx in self.transactions:
            if not tx.date.startswith(month):
                continue
            for line in tx.lines:
                if line.account_name == linked_coa_account:
                    total += line.dr - line.cr
        return money(total)

    def assign_to_envelope(self, month: str, category: str, amount: Decimal,
                            note: Optional[str] = None) -> Tuple[bool, str]:
        """Move money from Ready to Assign into an envelope."""
        try:
            datetime.strptime(month, "%Y-%m")
        except ValueError:
            return False, "Invalid month format (use YYYY-MM)."
        m_amount = money(amount)
        if m_amount <= ZERO:
            return False, "Amount must be positive."
        conn, cur = self._connect()
        try:
            now = datetime.now().isoformat(timespec="seconds")
            cur.execute(
                "INSERT INTO sql_budget_ledger "
                "(month, category_name, event_type, amount, related_id, note, created_at) "
                "VALUES (?, ?, 'assign', ?, NULL, ?, ?)",
                (month, category, str(m_amount), note, now)
            )
            conn.commit()
            return True, f"Assigned {m_amount} to {category}."
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()

    def unassign_from_envelope(self, month: str, category: str, amount: Decimal,
                                note: Optional[str] = None) -> Tuple[bool, str]:
        """Return money from an envelope to Ready to Assign."""
        try:
            datetime.strptime(month, "%Y-%m")
        except ValueError:
            return False, "Invalid month format (use YYYY-MM)."
        m_amount = money(amount)
        if m_amount <= ZERO:
            return False, "Amount must be positive."
        conn, cur = self._connect()
        try:
            now = datetime.now().isoformat(timespec="seconds")
            cur.execute(
                "INSERT INTO sql_budget_ledger "
                "(month, category_name, event_type, amount, related_id, note, created_at) "
                "VALUES (?, ?, 'unassign', ?, NULL, ?, ?)",
                (month, category, str(m_amount), note, now)
            )
            conn.commit()
            return True, f"Unassigned {m_amount} from {category}."
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()

    def move_budget_money(self, from_category: str, to_category: str,
                           amount: Decimal, month: str,
                           note: Optional[str] = None) -> Tuple[bool, str]:
        """Atomic move between envelopes, same month. Two rows sharing a related_id."""
        try:
            datetime.strptime(month, "%Y-%m")
        except ValueError:
            return False, "Invalid month format (use YYYY-MM)."
        if from_category == to_category:
            return False, "Source and destination envelopes must differ."
        m_amount = money(amount)
        if m_amount <= ZERO:
            return False, "Amount must be positive."
        conn, cur = self._connect()
        try:
            now = datetime.now().isoformat(timespec="seconds")
            cur.execute(
                "INSERT INTO sql_budget_ledger "
                "(month, category_name, event_type, amount, related_id, note, created_at) "
                "VALUES (?, ?, 'move_out', ?, NULL, ?, ?)",
                (month, from_category, str(m_amount), note, now)
            )
            move_out_id = cur.lastrowid
            cur.execute(
                "INSERT INTO sql_budget_ledger "
                "(month, category_name, event_type, amount, related_id, note, created_at) "
                "VALUES (?, ?, 'move_in', ?, ?, ?, ?)",
                (month, to_category, str(m_amount), move_out_id, note, now)
            )
            conn.commit()
            return True, f"Moved {m_amount} from {from_category} to {to_category}."
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()

    def set_income_plan(self, month: str, expected: Decimal, source: str,
                         derived: Optional[Decimal]) -> Tuple[bool, str]:
        """Upsert income plan for a month."""
        try:
            datetime.strptime(month, "%Y-%m")
        except ValueError:
            return False, "Invalid month format (use YYYY-MM)."
        if source not in ('last_month', 'avg_3mo', 'avg_6mo', 'manual'):
            return False, "Invalid income_source."
        m_expected = money(expected)
        m_derived = money(derived) if derived is not None else None
        conn, cur = self._connect()
        try:
            now = datetime.now().isoformat(timespec="seconds")
            cur.execute(
                "INSERT INTO sql_budget_income_plan "
                "(month, expected_amount, income_source, derived_amount, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(month) DO UPDATE SET "
                "expected_amount = excluded.expected_amount, "
                "income_source = excluded.income_source, "
                "derived_amount = excluded.derived_amount, "
                "updated_at = excluded.updated_at",
                (month, str(m_expected), source, str(m_derived) if m_derived else None, now, now)
            )
            conn.commit()
            return True, "Income plan saved."
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()

    def get_income_plan(self, month: str) -> Optional[Dict[str, Any]]:
        """Get income plan for a month."""
        conn, cur = self._connect()
        try:
            cur.execute(
                "SELECT month, expected_amount, income_source, derived_amount, created_at, updated_at "
                "FROM sql_budget_income_plan WHERE month = ?",
                (month,)
            )
            row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return {
            "month": row[0],
            "expected_amount": money(row[1]),
            "income_source": row[2],
            "derived_amount": money(row[3]) if row[3] else None,
            "created_at": row[4],
            "updated_at": row[5],
        }

    def get_budget_envelopes(self, include_inactive: bool = False) -> List[Dict[str, Any]]:
        """Get all budget envelopes."""
        conn, cur = self._connect()
        try:
            if include_inactive:
                cur.execute(
                    "SELECT category_name, linked_coa_account, rollover_policy, is_active "
                    "FROM sql_budget_envelopes ORDER BY category_name"
                )
            else:
                cur.execute(
                    "SELECT category_name, linked_coa_account, rollover_policy, is_active "
                    "FROM sql_budget_envelopes WHERE is_active = 1 ORDER BY category_name"
                )
            rows = cur.fetchall()
        finally:
            conn.close()
        return [
            {"category_name": r[0], "linked_coa_account": r[1], "rollover_policy": r[2], "is_active": bool(r[3])}
            for r in rows
        ]

    def upsert_budget_envelope(self, category: str, linked_coa_account: Optional[str],
                                rollover_policy: str, is_active: bool) -> Tuple[bool, str]:
        """Create or update a budget envelope."""
        if rollover_policy not in ('reset_monthly', 'carry_forward'):
            return False, "Invalid rollover_policy."
        conn, cur = self._connect()
        try:
            cur.execute(
                "INSERT INTO sql_budget_envelopes "
                "(category_name, linked_coa_account, rollover_policy, is_active) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(category_name) DO UPDATE SET "
                "linked_coa_account = excluded.linked_coa_account, "
                "rollover_policy = excluded.rollover_policy, "
                "is_active = excluded.is_active",
                (category, linked_coa_account, rollover_policy, 1 if is_active else 0)
            )
            conn.commit()
            return True, "Envelope saved."
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()

    def get_budget_ledger_rows(self, month: str) -> List[Dict[str, Any]]:
        """Get all budget ledger rows for a month."""
        conn, cur = self._connect()
        try:
            cur.execute(
                "SELECT id, month, category_name, event_type, amount, related_id, note, created_at "
                "FROM sql_budget_ledger WHERE month = ? ORDER BY id",
                (month,)
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        return [
            {"id": r[0], "month": r[1], "category_name": r[2], "event_type": r[3],
             "amount": money(r[4]), "related_id": r[5], "note": r[6], "created_at": r[7]}
            for r in rows
        ]

    def get_budget_ledger_rows_for_category(self, category: str) -> List[Dict[str, Any]]:
        """All rows for a single category, across all months, ordered by month ASC."""
        conn, cur = self._connect()
        try:
            cur.execute(
                "SELECT id, month, category_name, event_type, amount, related_id, note, created_at "
                "FROM sql_budget_ledger WHERE category_name = ? ORDER BY month, id",
                (category,)
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        return [
            {"id": r[0], "month": r[1], "category_name": r[2], "event_type": r[3],
             "amount": money(r[4]), "related_id": r[5], "note": r[6], "created_at": r[7]}
            for r in rows
        ]

    def spending_by_category(self, month: str) -> Dict[str, Decimal]:
        """Net spending per expense account in a month (debits minus credits).

        Accounts whose net is zero or negative are left out: a chart cannot
        draw a negative slice, and a fully voided expense should not appear.
        """
        net: Dict[str, Decimal] = {}
        for tx in self.transactions:
            if not tx.date.startswith(month):
                continue
            for line in tx.lines:
                acc = self.accounts[line.account_name]
                if acc.account_type == "Expense":
                    net[line.account_name] = net.get(line.account_name, ZERO) + line.dr - line.cr
        return {k: money(v) for k, v in net.items() if money(v) > ZERO}

    def balance_sheet(self) -> Dict[str, Dict[str, Any]]:
        result = {k: {"total": ZERO, "accounts": {}} for k in ["assets", "liabilities", "equity", "income", "expenses"]}
        for name, acc in self.accounts.items():
            bal = acc.get_balance()
            if acc.account_type == "Asset":
                result["assets"]["accounts"][name] = bal
                result["assets"]["total"] += bal
            elif acc.account_type == "Liability":
                result["liabilities"]["accounts"][name] = bal
                result["liabilities"]["total"] += bal
            elif acc.account_type == "Equity":
                result["equity"]["accounts"][name] = bal
                result["equity"]["total"] += bal
            elif acc.account_type == "Revenue":
                result["income"]["accounts"][name] = bal
                result["income"]["total"] += bal
            elif acc.account_type == "Expense":
                result["expenses"]["accounts"][name] = bal
                result["expenses"]["total"] += bal

        for k in result:
            result[k]["total"] = money(result[k]["total"])
        return result

    def balance_sheet_data(self, as_of_date: str) -> Dict[str, Any]:
        """
        Per-account balances as of a specific date, grouped by
        account_type -> subcategory -> report_line -> [(account, balance)].

        Differs from get_accounts_grouped_for_report() in two ways:
          - balances are computed as of a date cutoff, not current state
          - the leaf is (account_name, balance), not just the account name

        Uses running_balance_as_of() per account so the arithmetic is
        identical to what the Balance Sheet tab already shows for
        today's date, just evaluated at a historical cutoff.

        Returns:
          {
            "Asset": {
              "Current Assets": {
                "Cash and Cash Equivalents": [("Checking Account", Decimal("1000.00")), ...],
                ...
              },
              ...
            },
            ...
          }
        """
        try:
            datetime.strptime(as_of_date, "%Y-%m-%d")
        except ValueError:
            raise ValueError("Invalid as_of_date format (use YYYY-MM-DD).")

        result: Dict[str, Dict[str, Dict[str, List[Tuple[str, Decimal]]]]] = {}

        for name, acc in self.accounts.items():
            # Compute the balance as of the cutoff date
            bal = self.running_balance_as_of(name, as_of_date)

            subcat = acc.subcategory            # None if no report_line set
            report_line = acc.report_line       # None if unclassified

            by_sub = result.setdefault(acc.account_type, {})
            by_line = by_sub.setdefault(subcat, {})
            by_line.setdefault(report_line, []).append((name, bal))

        # Sort account names within each leaf
        for by_sub in result.values():
            for by_line in by_sub.values():
                for entries in by_line.values():
                    entries.sort(key=lambda t: t[0])

        return result

    def equity_statement_data(self, start_date: str, end_date: str) -> Dict[str, Any]:
        """
        Opening balance, period activity, and closing balance for each
        equity subcategory, broken down by account.

        Structure:
          {
            "subcategories": {
              "Contributed Capital": {
                "accounts": [
                  {"name": "Owner's Contributions",
                   "opening": Decimal, "movement": Decimal, "closing": Decimal},
                  ...
                ],
                "opening": Decimal,
                "movement": Decimal,
                "closing": Decimal,
              },
              ...
            },
            "total_opening": Decimal,
            "total_movement": Decimal,
            "total_closing": Decimal,
          }

        Opening balance is computed as of the day before `start_date`;
        closing as of `end_date`. Movement is closing minus opening.
        """
        for d in (start_date, end_date):
            try:
                datetime.strptime(d, "%Y-%m-%d")
            except ValueError:
                raise ValueError("Invalid date format (use YYYY-MM-DD).")

        # Opening = as of the day before start
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        opening_cutoff = (start_dt - timedelta(days=1)).strftime("%Y-%m-%d")

        # Collect Equity accounts grouped by subcategory
        subcategories: Dict[str, List[Dict[str, Any]]] = {}
        for name, acc in self.accounts.items():
            if acc.account_type != "Equity":
                continue
            subcat = acc.subcategory or "Unclassified"
            opening = self.running_balance_as_of(name, opening_cutoff)
            closing = self.running_balance_as_of(name, end_date)
            movement = money(closing - opening)
            if opening == ZERO and closing == ZERO and movement == ZERO:
                continue  # don't clutter the statement with untouched accounts
            subcategories.setdefault(subcat, []).append({
                "name": name,
                "opening": money(opening),
                "movement": movement,
                "closing": money(closing),
            })

        # Sort accounts within each subcategory alphabetically; sort
        # subcategory keys by the canonical order (from REPORT_LINE_METADATA)
        for accounts in subcategories.values():
            accounts.sort(key=lambda a: a["name"])

        # Canonical subcategory ordering for equity
        canonical_order = ["Contributed Capital", "Earnings", "Distributions"]
        ordered_keys = []
        for key in canonical_order:
            if key in subcategories:
                ordered_keys.append(key)
        extras = sorted(k for k in subcategories if k not in canonical_order and k != "Unclassified")
        ordered_keys.extend(extras)
        if "Unclassified" in subcategories:
            ordered_keys.append("Unclassified")

        # Compute subcategory totals
        total_opening = ZERO
        total_movement = ZERO
        total_closing = ZERO
        for key in ordered_keys:
            s_open = sum((a["opening"] for a in subcategories[key]), ZERO)
            s_move = sum((a["movement"] for a in subcategories[key]), ZERO)
            s_close = sum((a["closing"] for a in subcategories[key]), ZERO)
            subcategories[key] = {
                "accounts": subcategories[key],
                "opening": money(s_open),
                "movement": money(s_move),
                "closing": money(s_close),
            }
            total_opening += s_open
            total_movement += s_move
            total_closing += s_close

        # Preserve canonical ordering in the returned dict
        ordered_subcats = {k: subcategories[k] for k in ordered_keys}

        return {
            "subcategories": ordered_subcats,
            "total_opening": money(total_opening),
            "total_movement": money(total_movement),
            "total_closing": money(total_closing),
        }

    def import_ledger_csv(self, path: str) -> int:
        import csv
        conn, cur = self._connect()
        try:
            tx_groups = {}
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if not header:
                    return 0
                for row in reader:
                    if len(row) < 6:
                        continue
                    date, desc, acc_name, acc_type, dr_str, cr_str = row[0], row[1], row[2], row[3], row[4], row[5]
                    dr = money(dr_str) if dr_str.strip() else ZERO
                    cr = money(cr_str) if cr_str.strip() else ZERO
                    acc_type = acc_type.strip().capitalize()
                    if acc_name not in self.accounts:
                        cur.execute("INSERT OR IGNORE INTO sql_accounts (name, account_type) VALUES (?, ?)", (acc_name, acc_type))
                        self.accounts[acc_name] = Account(acc_name, acc_type)
                    tx_groups.setdefault((date, desc), []).append((acc_name, dr, cr))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        imported = 0
        for (date, desc), lines in tx_groups.items():
            ok, _ = self.record_transaction(date, desc, lines, persist=True)
            if ok:
                imported += 1
        return imported

    def mark_reconciled(self, tx_id: int, reconciled: bool):
        for tx in self.transactions:
            if tx.id == tx_id:
                self.assert_tx_unlocked(tx)   # raises PeriodLockedError (a ValueError)
                tx.reconciled = reconciled
                conn, cur = self._connect()
                try:
                    cur.execute("UPDATE sql_transactions SET reconciled = ? WHERE id = ?", (1 if reconciled else 0, tx_id))
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()

                if reconciled:
                    # Reconciling is one of the two points a staged
                    # merchant_draft gets promoted (Add/Edit confirm is
                    # the other, not yet wired). Best-effort and silent:
                    # most lines here won't have a draft at all -- the
                    # bank/asset leg never gets one -- and that's an
                    # expected no-op, not an error condition.
                    for line in tx.lines:
                        if line.id is None:
                            continue
                        try:
                            self.confirm_merchant_draft(tx_id, line.id, line.account_name)
                        except Exception:
                            logger.exception(f"Merchant learning: failed to confirm draft for tx {tx_id} line {line.id}")

                    # Recurring charges: this is the same confirmation
                    # point, and it is where new recurring evidence
                    # enters the subsystem. Re-running detection here
                    # extends staged candidates and advances confirmed
                    # patterns' occurrence counts and next-expected
                    # dates. It never promotes a draft into a pattern
                    # -- that stays a deliberate user action in the
                    # Recurring tab, because a recurring claim is
                    # about the future and reconciling one charge does
                    # not vouch for it.
                    try:
                        self.refresh_recurring_candidates()
                    except Exception:
                        logger.exception("Recurring detection: refresh failed after reconcile")
                return
        raise ValueError("Transaction not found.")

    def get_unreconciled_transactions(self) -> List[Transaction]:
        return [t for t in self.transactions if not t.reconciled]

    def trial_balance(self) -> List[Dict[str, Any]]:
        rows = []
        for name, acc in self.accounts.items():
            bal = acc.get_balance()
            norm = acc.NORMAL_BALANCES[acc.account_type]
            if norm == "DR":
                dr = bal if bal >= ZERO else ZERO
                cr = -bal if bal < ZERO else ZERO
            else:
                cr = bal if bal >= ZERO else ZERO
                dr = -bal if bal < ZERO else ZERO
            rows.append({"name": name, "type": acc.account_type, "dr": money(dr), "cr": money(cr)})
        return rows

    def trial_balance_data(self, as_of_date: str) -> Dict[str, Any]:
        """
        Raw data for a formal trial balance as of a given date.

        Returns accounts grouped by account_type, each entry as
        (account_name, dr_balance, cr_balance) where the two columns are
        zero for the side the account doesn't naturally sit on.

        The DR/CR split uses the account's normal balance convention:
        a DR-normal account with a positive balance contributes to the
        Debit column; a CR-normal account with a positive balance
        contributes to the Credit column. A negative balance on either
        type contributes to the opposite column, preserving the
        accounting identity that total debits equal total credits.
        """
        try:
            datetime.strptime(as_of_date, "%Y-%m-%d")
        except ValueError:
            raise ValueError("Invalid as_of_date format (use YYYY-MM-DD).")

        grouped: Dict[str, List[Tuple[str, Decimal, Decimal]]] = {
            "Asset": [],
            "Liability": [],
            "Equity": [],
            "Revenue": [],
            "Expense": [],
        }

        total_dr = ZERO
        total_cr = ZERO

        for name, acc in self.accounts.items():
            bal = self.running_balance_as_of(name, as_of_date)
            if bal == ZERO:
                continue  # accounts with no activity don't belong on a trial balance

            norm = acc.NORMAL_BALANCES[acc.account_type]
            if norm == "DR":
                dr = bal if bal >= ZERO else ZERO
                cr = -bal if bal < ZERO else ZERO
            else:
                cr = bal if bal >= ZERO else ZERO
                dr = -bal if bal < ZERO else ZERO

            grouped[acc.account_type].append((name, money(dr), money(cr)))
            total_dr += dr
            total_cr += cr

        for entries in grouped.values():
            entries.sort(key=lambda t: t[0])

        return {
            "as_of_date": as_of_date,
            "groups": grouped,
            "total_dr": money(total_dr),
            "total_cr": money(total_cr),
            "balanced": abs(total_dr - total_cr) < Decimal("0.005"),
            "difference": money(total_dr - total_cr),
        }

    def export_journal_csv(self, path: str):
        import csv
        rows = []
        for tx in self.transactions:
            for line in tx.lines:
                rows.append([
                    _sanitize_for_csv(tx.date),
                    _sanitize_for_csv(tx.description),
                    _sanitize_for_csv(line.account_name),
                    _sanitize_for_csv(self.accounts[line.account_name].account_type),
                    line.dr if line.dr > ZERO else "",
                    line.cr if line.cr > ZERO else "",
                ])
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Date", "Description", "Account Name", "Account Type", "Debit ($)", "Credit ($)"])
            w.writerows(rows)

    def add_category_rule(self, keyword: str, category: str, case_sensitive: bool = False, enabled: bool = True) -> Tuple[bool, str]:
        if category not in self.accounts:
            return False, f"Category '{category}' does not exist."
        conn, cur = self._connect()
        try:
            cur.execute("INSERT INTO sql_category_rules (keyword, category, case_sensitive, enabled) VALUES (?, ?, ?, ?)", (keyword, category, 1 if case_sensitive else 0, 1 if enabled else 0))
            rid = cur.lastrowid
            conn.commit()
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()
        self.category_rules.append(CategoryRule(rid, keyword, category, case_sensitive, enabled))
        return True, "Rule created."

    def update_category_rule(self, rule_id: int, keyword: str, category: str, case_sensitive: bool, enabled: bool) -> Tuple[bool, str]:
        if category not in self.accounts:
            return False, f"Category '{category}' does not exist."
        if self.accounts[category].account_type != "Expense":
            return False, "Category must be an Expense account."
        conn, cur = self._connect()
        try:
            cur.execute("UPDATE sql_category_rules SET keyword = ?, category = ?, case_sensitive = ?, enabled = ? WHERE id = ?", (keyword, category, 1 if case_sensitive else 0, 1 if enabled else 0, rule_id))
            if cur.rowcount == 0:
                return False, "Rule not found."
            conn.commit()
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()
        for rule in self.category_rules:
            if rule.id == rule_id:
                rule.keyword = keyword
                rule.category = category
                rule.case_sensitive = case_sensitive
                rule.enabled = enabled
                break
        return True, "Rule updated."

    def delete_category_rule(self, rule_id: int) -> Tuple[bool, str]:
        conn, cur = self._connect()
        try:
            cur.execute("DELETE FROM sql_category_rules WHERE id = ?", (rule_id,))
            if cur.rowcount == 0:
                return False, "Rule not found."
            conn.commit()
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()
        self.category_rules = [r for r in self.category_rules if r.id != rule_id]
        return True, "Rule deleted."

    def get_category_rules(self) -> List[CategoryRule]:
        return list(self.category_rules)

    def apply_category_rules(self, description: str) -> Optional[str]:
        for rule in self.category_rules:
            if not rule.enabled:
                continue
            kw = rule.keyword if rule.case_sensitive else rule.keyword.lower()
            desc = description if rule.case_sensitive else description.lower()
            if kw in desc:
                return rule.category
        return None

    # ---------------- Merchant Memory ----------------

    def record_merchant_draft(self, tx_id: int, line_id: int, raw_string: str,
                               merchant_key: str, merchant_display: str,
                               category_suggested: Optional[str]) -> Tuple[bool, str]:
        """
        Stage a merchant normalization result for one transaction line.
        Zero-risk: never touches ledger math, only the draft table used to
        seed sql_merchant_pattern once the transaction is confirmed. Safe
        to call more than once for the same (tx_id, line_id) -- it
        replaces rather than duplicates the draft row.
        """
        conn, cur = self._connect()
        try:
            cur.execute(
                "INSERT INTO sql_merchant_draft "
                "(tx_id, line_id, raw_string, merchant_key, merchant_display, category_suggested, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(tx_id, line_id) DO UPDATE SET "
                "raw_string = excluded.raw_string, "
                "merchant_key = excluded.merchant_key, "
                "merchant_display = excluded.merchant_display, "
                "category_suggested = excluded.category_suggested, "
                "created_at = excluded.created_at",
                (tx_id, line_id, raw_string, merchant_key, merchant_display,
                 category_suggested, datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()
        return True, "Draft recorded."

    def confirm_merchant_draft(self, tx_id: int, line_id: int, final_category: str) -> Tuple[bool, str]:
        """
        Promote a staged draft into the permanent learned pattern once the
        user confirms the transaction (Add/Edit) or reconciles it. Always
        feeds the raw occurrence counts with real data, regardless of
        whether final_category matches the original suggestion -- but
        never touches rating; a correction during review is a data point,
        not a deliberate Green/Red vouch.

        Blacklist status is deliberately NOT checked here: a blacklisted
        (merchant_key, category) pair still silently accumulates counts in
        the background so it can re-cross the suggestion threshold someday
        -- the blacklist only suppresses it from being *surfaced*
        (see get_merchant_patterns), not from being *counted*.
        """
        conn, cur = self._connect()
        try:
            cur.execute(
                "SELECT merchant_key, merchant_display FROM sql_merchant_draft "
                "WHERE tx_id = ? AND line_id = ?",
                (tx_id, line_id),
            )
            row = cur.fetchone()
            if not row:
                return False, "No draft found for this line."
            merchant_key, merchant_display = row
            if not merchant_key:
                cur.execute("DELETE FROM sql_merchant_draft WHERE tx_id = ? AND line_id = ?", (tx_id, line_id))
                conn.commit()
                return True, "Draft had no merchant_key; discarded."

            now = datetime.now().isoformat(timespec="seconds")
            cur.execute(
                "INSERT INTO sql_merchant_pattern "
                "(merchant_key, merchant_display, category, occurrences, rating, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, 1, 'yellow', ?, ?) "
                "ON CONFLICT(merchant_key, category) DO UPDATE SET "
                "occurrences = occurrences + 1, "
                "merchant_display = excluded.merchant_display, "
                "last_seen_at = excluded.last_seen_at",
                (merchant_key, merchant_display, final_category, now, now),
            )
            cur.execute("DELETE FROM sql_merchant_draft WHERE tx_id = ? AND line_id = ?", (tx_id, line_id))
            conn.commit()
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()
        return True, "Pattern updated."

    def rate_pattern(self, merchant_key: str, category: str, rating: str, mode: str = "pro") -> Tuple[bool, str]:
        """
        Apply a Green/Yellow rating. In Pro Mode this touches exactly one
        (merchant_key, category) row. In Simple Mode it fans out to every
        granular pattern sharing that pair's merchant_display -- a
        deliberate one-to-many write, not a single-row update. Storage is
        always granular regardless of mode; this is the only place mode
        actually changes behavior.
        """
        if rating not in ("green", "yellow"):
            return False, "rating must be 'green' or 'yellow' (use reject_pattern for Red)."

        conn, cur = self._connect()
        try:
            if mode == "simple":
                cur.execute(
                    "SELECT merchant_display FROM sql_merchant_pattern WHERE merchant_key = ? AND category = ?",
                    (merchant_key, category),
                )
                row = cur.fetchone()
                if not row:
                    return False, "Pattern not found."
                cur.execute(
                    "UPDATE sql_merchant_pattern SET rating = ? WHERE merchant_display = ? AND category = ?",
                    (rating, row[0], category),
                )
            else:
                cur.execute(
                    "UPDATE sql_merchant_pattern SET rating = ? WHERE merchant_key = ? AND category = ?",
                    (rating, merchant_key, category),
                )
            affected = cur.rowcount
            if affected == 0:
                conn.rollback()
                return False, "Pattern not found."
            conn.commit()
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()
        return True, f"Rated {affected} pattern(s) '{rating}'."

    def reject_pattern(self, merchant_key: str, category: str, mode: str = "pro") -> Tuple[bool, str]:
        """
        Red: 'this is wrong, don't count it.' Deletes the accumulated
        counts for the affected pattern(s) and blacklists each
        (merchant_key, category) pair individually so it can't quietly
        resurface once occurrences pile back up. Fans out the same way
        Green/Yellow do in Simple Mode (one write per underlying
        merchant_key sharing the pooled merchant_display+category) -- but
        the blacklist rows themselves stay per-granular-key, never
        per-display, so "Blacklisted Patterns" in Category Rules tab
        always shows real, individually reversible granular entries even
        if the user never saw them one at a time.
        """
        conn, cur = self._connect()
        try:
            if mode == "simple":
                cur.execute(
                    "SELECT merchant_display FROM sql_merchant_pattern WHERE merchant_key = ? AND category = ?",
                    (merchant_key, category),
                )
                row = cur.fetchone()
                if not row:
                    return False, "Pattern not found."
                cur.execute(
                    "SELECT merchant_key FROM sql_merchant_pattern WHERE merchant_display = ? AND category = ?",
                    (row[0], category),
                )
                target_keys = [r[0] for r in cur.fetchall()]
            else:
                target_keys = [merchant_key]

            if not target_keys:
                return False, "Pattern not found."

            now = datetime.now().isoformat(timespec="seconds")
            for key in target_keys:
                cur.execute("DELETE FROM sql_merchant_pattern WHERE merchant_key = ? AND category = ?", (key, category))
                cur.execute(
                    "INSERT OR IGNORE INTO sql_merchant_blacklist (merchant_key, category, blacklisted_at) VALUES (?, ?, ?)",
                    (key, category, now),
                )
            conn.commit()
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()
        return True, f"Blacklisted {len(target_keys)} pattern(s)."

    def unblacklist_pattern(self, merchant_key: str, category: str) -> Tuple[bool, str]:
        """
        Reverse a blacklist entry. Always single-row/granular, even for
        patterns originally blacklisted via a Simple Mode pooled Red --
        visibility and reversibility have to operate at the level the
        user can actually see them at in the Blacklisted Patterns list.
        """
        conn, cur = self._connect()
        try:
            cur.execute(
                "DELETE FROM sql_merchant_blacklist WHERE merchant_key = ? AND category = ?",
                (merchant_key, category),
            )
            if cur.rowcount == 0:
                return False, "Blacklist entry not found."
            conn.commit()
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()
        return True, "Un-blacklisted."

    def get_merchant_blacklist(self) -> List[Dict[str, Any]]:
        conn, cur = self._connect()
        try:
            cur.execute(
                "SELECT merchant_key, category, blacklisted_at FROM sql_merchant_blacklist "
                "ORDER BY blacklisted_at DESC"
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        return [{"merchant_key": r[0], "category": r[1], "blacklisted_at": r[2]} for r in rows]

    def build_promotion_rule_defaults(self, merchant_key: str, category: str, mode: str = "pro") -> Optional[Dict[str, Any]]:
        """
        Pre-fill values for the Add/Edit Category Rule form when the user
        clicks 'Promote to Rule'. keyword is merchant_key in Pro Mode
        (store-specific) or merchant_display in Simple Mode -- since
        merchant_display is always a substring of every granular
        description under it, one Simple Mode rule naturally covers every
        location with no fan-out logic needed for rule creation itself.
        """
        conn, cur = self._connect()
        try:
            cur.execute(
                "SELECT merchant_display FROM sql_merchant_pattern WHERE merchant_key = ? AND category = ?",
                (merchant_key, category),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            return None
        keyword = row[0] if mode == "simple" else merchant_key
        return {"keyword": keyword, "category": category, "case_sensitive": False, "enabled": True}

    def promote_pattern(self, merchant_key: str, category: str, rule_id: int, mode: str = "pro") -> Tuple[bool, str]:
        """
        Stamp promoted_rule_id after a Category Rule has been created from
        this pattern (see build_promotion_rule_defaults for the pre-fill
        step, called just before this). Fans out in Simple Mode -- same
        underlying engine as rating -- so a pooled promotion marks every
        granular pattern it actually covers as already-ruled. This is
        what makes Simple -> Pro non-destructive: the granular patterns
        already exist and already know they're covered, so Pro Mode never
        has to re-accumulate ("re-train") anything.
        """
        conn, cur = self._connect()
        try:
            if mode == "simple":
                cur.execute(
                    "SELECT merchant_display FROM sql_merchant_pattern WHERE merchant_key = ? AND category = ?",
                    (merchant_key, category),
                )
                row = cur.fetchone()
                if not row:
                    return False, "Pattern not found."
                cur.execute(
                    "UPDATE sql_merchant_pattern SET promoted_rule_id = ? WHERE merchant_display = ? AND category = ?",
                    (rule_id, row[0], category),
                )
            else:
                cur.execute(
                    "UPDATE sql_merchant_pattern SET promoted_rule_id = ? WHERE merchant_key = ? AND category = ?",
                    (rule_id, merchant_key, category),
                )
            affected = cur.rowcount
            if affected == 0:
                conn.rollback()
                return False, "Pattern not found."
            conn.commit()
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()
        return True, f"Marked {affected} pattern(s) as promoted."

    @staticmethod
    def _confidence_row(merchant_key, merchant_display, category, count_this_category,
                         count_total, rating, promoted_rule_id) -> Dict[str, Any]:
        consistency_ratio = (count_this_category / count_total) if count_total else 0.0
        volume_factor = min(1.0, count_this_category / MERCHANT_MEMORY_VOLUME_CAP)
        base_confidence = consistency_ratio * volume_factor
        modifier = MERCHANT_MEMORY_RATING_MODIFIERS.get(rating, 1.0)
        # Deliberately NOT clamped to 1.0 -- a deliberate Green vouch can
        # push a pattern over threshold even where raw math alone
        # wouldn't. Intentional, not a bug.
        final_confidence = base_confidence * modifier

        return {
            "merchant_key": merchant_key,
            "merchant_display": merchant_display,
            "category": category,
            "occurrences": count_this_category,
            "count_total": count_total,
            "consistency_ratio": consistency_ratio,
            "rating": rating,
            "final_confidence": final_confidence,
            "can_promote": final_confidence >= MERCHANT_MEMORY_PROMOTE_THRESHOLD,
            "promoted_rule_id": promoted_rule_id,
        }

    def get_merchant_patterns(self, mode: str = "pro", min_confidence: Optional[float] = None) -> List[Dict[str, Any]]:
        """
        Live-computed view of learned patterns -- never a cached score.
        Pro Mode: one row per (merchant_key, category). Simple Mode: rows
        sharing (merchant_display, category) are pooled -- occurrences
        summed, rating is 'green' only if every underlying granular row is
        'green' (a straggler direct-Pro rating shouldn't overstate a
        pooled view's confidence). Blacklisted (merchant_key, category)
        pairs are excluded entirely; patterns below
        MERCHANT_MEMORY_MIN_OCCURRENCES never surface, regardless of how
        high a raw ratio would otherwise compute.
        """
        conn, cur = self._connect()
        try:
            cur.execute("SELECT merchant_key, category FROM sql_merchant_blacklist")
            blacklisted = set(cur.fetchall())

            cur.execute(
                "SELECT merchant_key, merchant_display, category, occurrences, rating, promoted_rule_id "
                "FROM sql_merchant_pattern"
            )
            raw_rows = cur.fetchall()
        finally:
            conn.close()

        raw_rows = [r for r in raw_rows if (r[0], r[2]) not in blacklisted]

        # merchant_key -> total occurrences across ALL its categories --
        # consistency_ratio's denominator, computed here, never stored.
        key_totals: Dict[str, int] = {}
        for merchant_key, _disp, _cat, occurrences, _rating, _promo in raw_rows:
            key_totals[merchant_key] = key_totals.get(merchant_key, 0) + occurrences

        results: List[Dict[str, Any]] = []

        if mode == "simple":
            pooled: Dict[Tuple[str, str], Dict[str, Any]] = {}
            for merchant_key, merchant_display, category, occurrences, rating, promoted_rule_id in raw_rows:
                entry = pooled.setdefault((merchant_display, category), {
                    "occurrences": 0, "all_green": True,
                    "promoted_rule_id": None, "merchant_keys": [],
                })
                entry["occurrences"] += occurrences
                entry["all_green"] = entry["all_green"] and (rating == "green")
                entry["merchant_keys"].append(merchant_key)
                if promoted_rule_id is not None:
                    entry["promoted_rule_id"] = promoted_rule_id

            for (merchant_display, category), entry in pooled.items():
                total = sum(key_totals[k] for k in set(entry["merchant_keys"]))
                row = self._confidence_row(
                    merchant_key=None, merchant_display=merchant_display, category=category,
                    count_this_category=entry["occurrences"], count_total=total,
                    rating="green" if entry["all_green"] else "yellow",
                    promoted_rule_id=entry["promoted_rule_id"],
                )
                # Read-path enhancement: the pooled row covers granular
                # patterns; expose them so Simple Mode can disclose the
                # fan-out scope and the user can see/re-rate individual rows.
                # No storage change; existing consumers don't read these keys.
                row["merchant_keys"] = sorted(set(entry["merchant_keys"]))
                row["pooled_count"] = len(row["merchant_keys"])
                results.append(row)
        else:
            for merchant_key, merchant_display, category, occurrences, rating, promoted_rule_id in raw_rows:
                results.append(self._confidence_row(
                    merchant_key=merchant_key, merchant_display=merchant_display, category=category,
                    count_this_category=occurrences, count_total=key_totals[merchant_key],
                    rating=rating, promoted_rule_id=promoted_rule_id,
                ))

        results = [r for r in results if r["occurrences"] >= MERCHANT_MEMORY_MIN_OCCURRENCES]
        if min_confidence is not None:
            results = [r for r in results if r["final_confidence"] >= min_confidence]
        return sorted(results, key=lambda r: r["final_confidence"], reverse=True)

    def get_last_transaction(self) -> Optional[Transaction]:
        """
        The most recently posted transaction, with real line ids attached.
        record_transaction() intentionally still returns (bool, str) --
        changing that would ripple through every existing caller -- so
        this is how a caller that needs the new tx_id/line ids (e.g. to
        record a merchant draft right after posting) gets them, without
        touching that contract. Only meaningful immediately after a
        successful, non-batched record_transaction() call: during an
        active sync-transaction batch, newly posted transactions aren't
        appended to self.transactions until the whole batch commits.
        """
        return self.transactions[-1] if self.transactions else None

    def get_pattern(self, merchant_key: str, category: str) -> Optional[Dict[str, Any]]:
        """
        Confidence view for one exact (merchant_key, category) pair.
        Used by UI surfaces that already know which category a
        transaction is about to post to (e.g. the reconcile/merge
        dialog's confidence tag) and want live confidence for
        specifically that pairing -- as opposed to
        get_best_suggestion_for_merchant's "what's the best category
        overall for this merchant." Returns None if there's no pattern
        row for this pair, or if it's blacklisted.
        """
        conn, cur = self._connect()
        try:
            cur.execute(
                "SELECT occurrences, rating, promoted_rule_id FROM sql_merchant_pattern "
                "WHERE merchant_key = ? AND category = ?",
                (merchant_key, category),
            )
            row = cur.fetchone()
            if not row:
                return None
            cur.execute(
                "SELECT 1 FROM sql_merchant_blacklist WHERE merchant_key = ? AND category = ?",
                (merchant_key, category),
            )
            if cur.fetchone():
                return None
            cur.execute(
                "SELECT SUM(occurrences) FROM sql_merchant_pattern WHERE merchant_key = ?",
                (merchant_key,),
            )
            count_total = cur.fetchone()[0] or 0
        finally:
            conn.close()
        occurrences, rating, promoted_rule_id = row
        if occurrences < MERCHANT_MEMORY_MIN_OCCURRENCES:
            return None
        return self._confidence_row(merchant_key, None, category, occurrences, count_total, rating, promoted_rule_id)

    def get_best_suggestion_for_merchant(self, merchant_key: str) -> Optional[Dict[str, Any]]:
        """
        Best current merchant-memory suggestion for one granular
        merchant_key -- used to populate a merchant_draft's
        category_suggested at import time. Deliberately independent of
        Simple/Pro mode and of category_rules: those are separate
        signals from "what has this exact merchant_key been categorized
        as before." Returns None if nothing clears the minimum
        occurrence floor, or every candidate category for this
        merchant_key is blacklisted.
        """
        if not merchant_key:
            return None
        conn, cur = self._connect()
        try:
            cur.execute(
                "SELECT category, occurrences, rating, promoted_rule_id FROM sql_merchant_pattern "
                "WHERE merchant_key = ?",
                (merchant_key,),
            )
            rows = cur.fetchall()
            if not rows:
                return None
            cur.execute(
                "SELECT category FROM sql_merchant_blacklist WHERE merchant_key = ?",
                (merchant_key,),
            )
            blacklisted_categories = {r[0] for r in cur.fetchall()}
        finally:
            conn.close()

        rows = [r for r in rows if r[0] not in blacklisted_categories]
        if not rows:
            return None
        count_total = sum(r[1] for r in rows)
        candidates = [
            self._confidence_row(merchant_key, None, category, occurrences, count_total, rating, promoted_rule_id)
            for category, occurrences, rating, promoted_rule_id in rows
        ]
        candidates = [c for c in candidates if c["occurrences"] >= MERCHANT_MEMORY_MIN_OCCURRENCES]
        if not candidates:
            return None
        return max(candidates, key=lambda c: c["final_confidence"])

    # ---------------- Recurring Charges ----------------
    #
    # Mirrors the Merchant Memory lifecycle -- stage a draft, let the
    # user confirm it, promote it to a pattern, blacklist it when it's
    # a false positive, and always keep the blacklist reversible --
    # but over its own tables. The two subsystems deliberately do NOT
    # share tables: sql_merchant_pattern is keyed
    # UNIQUE(merchant_key, category) and its confidence denominator is
    # SUM(occurrences) per merchant_key, so folding recurring rows
    # into it would both lose multiple cadences for one merchant and
    # silently corrupt the merchant-memory confidence score.

    def _recurring_charge_rows(self) -> List[Dict[str, Any]]:
        """
        Build the plain-data evidence the recurring engine consumes.

        One row per (transaction, expense category): a split
        transaction that touches the same category twice is merged into
        a single occurrence, so a charge is counted once rather than
        once per line.

        Voided transactions are skipped. The ledger keeps them for
        audit and posts an explicit reversal alongside them, so
        counting a voided charge would teach the detector that a
        cancelled subscription recurs.
        """
        rows: List[Dict[str, Any]] = []
        for tx in self.transactions:
            if tx.is_voided:
                continue

            amounts: Dict[str, Decimal] = {}
            for line in tx.lines:
                account = self.accounts.get(line.account_name)
                if account is None or account.account_type != "Expense":
                    continue
                if line.dr <= 0:
                    continue
                amounts[line.account_name] = amounts.get(line.account_name, ZERO) + line.dr
            if not amounts:
                continue

            merchant_key, merchant_display = normalize_merchant(tx.description or "")
            if not merchant_key:
                continue

            for category, amount in amounts.items():
                if amount <= 0:
                    continue
                rows.append({
                    "tx_id": tx.id,
                    "date": tx.date,
                    "merchant_key": merchant_key,
                    "merchant_display": merchant_display,
                    "category": category,
                    "amount": amount,
                })
        return rows

    def refresh_recurring_candidates(self) -> List[Dict[str, Any]]:
        """
        Re-run recurring detection and stage or refresh what it finds.

        This is where new *evidence* enters the recurring subsystem --
        the counterpart to confirm_merchant_draft being called from
        mark_reconciled(). Reconciling a transaction can extend a
        staged candidate's evidence set, or advance an already
        confirmed pattern's occurrence count and next-expected date.

        What it deliberately does NOT do is promote a draft into a
        pattern. A recurring claim is about the future ("this will
        charge again next month"), so reconciling one charge does not
        vouch for it -- the user confirms that in the Recurring tab.

        Never touches the accounting ledger, never changes a rating,
        and never creates a pattern on its own.
        """
        try:
            candidates = detect_recurring_candidates(self._recurring_charge_rows())
        except Exception:
            logger.exception("Recurring detection: candidate scan failed")
            return []

        blacklisted = {
            (r["merchant_key"], r["category"], r["interval"])
            for r in self.get_recurring_blacklist()
        }
        confirmed = {
            (r["merchant_key"], r["category"], r["interval"])
            for r in self.get_recurring_patterns()
        }

        staged = set()
        for cand in candidates:
            key = (cand["merchant_key"], cand["category"], cand["interval"])
            if key in blacklisted:
                continue
            staged.add(key)
            if key in confirmed:
                # A confirmed pattern is not re-suggested; it is
                # refreshed, so a new matching charge advances its
                # occurrence count and next-expected date. The rating
                # is never touched here -- exactly as Merchant Memory
                # leaves a rating alone when the counts move.
                self._refresh_confirmed_pattern(cand)
            else:
                self.record_recurring_draft(cand)

        # Drop staged candidates the evidence no longer supports, so
        # the review list always reflects what the detector actually
        # believes right now.
        self._prune_recurring_drafts(staged)
        return candidates

    def _refresh_confirmed_pattern(self, candidate: Dict[str, Any]) -> None:
        """Advance a confirmed pattern's statistics from fresh evidence."""
        conn, cur = self._connect()
        try:
            cur.execute(
                "UPDATE sql_recurring_pattern SET "
                "expected_amount = ?, amount_variance = ?, expected_next_date = ?, "
                "occurrences = ?, interval_regularity = ?, last_seen_at = ?, updated_at = ? "
                "WHERE merchant_key = ? AND category = ? AND interval = ? "
                "AND source = 'auto_detected'",
                (
                    str(candidate["expected_amount"]),
                    str(candidate["amount_variance"]),
                    candidate["expected_next_date"],
                    candidate["occurrences"],
                    candidate["interval_regularity"],
                    datetime.now().isoformat(timespec="seconds"),
                    datetime.now().isoformat(timespec="seconds"),
                    candidate["merchant_key"],
                    candidate["category"],
                    candidate["interval"],
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
        finally:
            conn.close()

    def _prune_recurring_drafts(self, keep: set) -> None:
        """Delete staged candidates that are no longer supported by evidence."""
        conn, cur = self._connect()
        try:
            cur.execute("SELECT merchant_key, category, interval FROM sql_recurring_draft")
            stale = [r for r in cur.fetchall() if (r[0], r[1], r[2]) not in keep]
            for merchant_key, category, interval in stale:
                cur.execute(
                    "DELETE FROM sql_recurring_draft "
                    "WHERE merchant_key = ? AND category = ? AND interval = ?",
                    (merchant_key, category, interval),
                )
            conn.commit()
        except Exception:
            conn.rollback()
        finally:
            conn.close()

    def record_recurring_draft(self, candidate: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Stage a detected candidate for review.

        Mirrors record_merchant_draft: staging only, zero risk, never
        touches ledger math, and safe to call repeatedly for the same
        triple -- it refreshes the row rather than duplicating it.

        Takes a candidate dict as produced by
        recurring_engine.detect_recurring_candidates().
        """
        conn, cur = self._connect()
        try:
            cur.execute(
                "INSERT INTO sql_recurring_draft "
                "(merchant_key, merchant_display, category, interval, expected_amount, "
                " amount_variance, expected_next_date, occurrences, interval_regularity, "
                " evidence_dates, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(merchant_key, category, interval) DO UPDATE SET "
                "merchant_display = excluded.merchant_display, "
                "expected_amount = excluded.expected_amount, "
                "amount_variance = excluded.amount_variance, "
                "expected_next_date = excluded.expected_next_date, "
                "occurrences = excluded.occurrences, "
                "interval_regularity = excluded.interval_regularity, "
                "evidence_dates = excluded.evidence_dates, "
                "created_at = excluded.created_at",
                (
                    candidate["merchant_key"],
                    candidate["merchant_display"],
                    candidate["category"],
                    candidate["interval"],
                    str(candidate["expected_amount"]),
                    str(candidate["amount_variance"]),
                    candidate["expected_next_date"],
                    candidate["occurrences"],
                    candidate["interval_regularity"],
                    json.dumps(candidate.get("evidence_dates") or []),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()
        return True, "Recurring candidate staged for review."

    def confirm_recurring_draft(self, merchant_key: str, category: str, interval: str) -> Tuple[bool, str]:
        """
        Promote a staged candidate into a confirmed pattern.

        The user act that makes a recurring claim real. Mirrors
        confirm_merchant_draft: the draft row is consumed and the
        pattern row is created -- but, exactly as in Merchant Memory,
        the rating is never touched here. A promotion is a data point,
        not a Green vouch; the user rates the pattern separately.
        """
        conn, cur = self._connect()
        try:
            cur.execute(
                "SELECT merchant_display, expected_amount, amount_variance, expected_next_date, "
                "occurrences, interval_regularity FROM sql_recurring_draft "
                "WHERE merchant_key = ? AND category = ? AND interval = ?",
                (merchant_key, category, interval),
            )
            row = cur.fetchone()
            if not row:
                return False, "No recurring candidate found for this pattern."

            (merchant_display, expected_amount, amount_variance,
             expected_next_date, occurrences, interval_regularity) = row

            now = datetime.now().isoformat(timespec="seconds")
            cur.execute(
                "INSERT INTO sql_recurring_pattern "
                "(merchant_key, merchant_display, category, interval, expected_amount, "
                " amount_variance, expected_next_date, occurrences, interval_regularity, "
                " rating, user_declared, source, first_seen_at, last_seen_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'yellow', 0, 'auto_detected', ?, ?, ?, ?) "
                "ON CONFLICT(merchant_key, category, interval) DO UPDATE SET "
                "merchant_display = excluded.merchant_display, "
                "expected_amount = excluded.expected_amount, "
                "amount_variance = excluded.amount_variance, "
                "expected_next_date = excluded.expected_next_date, "
                "occurrences = excluded.occurrences, "
                "interval_regularity = excluded.interval_regularity, "
                "last_seen_at = excluded.last_seen_at, "
                "updated_at = excluded.updated_at",
                (merchant_key, merchant_display, category, interval, expected_amount,
                 amount_variance, expected_next_date, occurrences, interval_regularity,
                 now, now, now, now),
            )
            cur.execute(
                "DELETE FROM sql_recurring_draft "
                "WHERE merchant_key = ? AND category = ? AND interval = ?",
                (merchant_key, category, interval),
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()
        return True, "Recurring pattern confirmed."

    def rate_recurring_pattern(self, merchant_key: str, category: str, interval: str,
                               rating: str) -> Tuple[bool, str]:
        """
        Apply a Green/Yellow rating. Mirrors rate_pattern: Red is not a
        rating here, it is reject_recurring.
        """
        if rating not in ("green", "yellow"):
            return False, "rating must be 'green' or 'yellow' (use reject_recurring for Red)."

        conn, cur = self._connect()
        try:
            cur.execute(
                "UPDATE sql_recurring_pattern SET rating = ?, updated_at = ? "
                "WHERE merchant_key = ? AND category = ? AND interval = ?",
                (rating, datetime.now().isoformat(timespec="seconds"),
                 merchant_key, category, interval),
            )
            affected = cur.rowcount
            if affected == 0:
                conn.rollback()
                return False, "Recurring pattern not found."
            conn.commit()
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()
        return True, f"Rated {affected} pattern(s) '{rating}'."

    def reject_recurring(self, merchant_key: str, category: str, interval: str) -> Tuple[bool, str]:
        """
        Red: 'this is not recurring.' Deletes the pattern row and any
        staged draft, and blacklists the triple so it cannot quietly
        resurface once the evidence piles back up.

        Mirrors reject_pattern, with one addition: a recurring
        candidate can be dismissed before it is ever promoted, so this
        also clears a staged draft. Either way the triple ends up
        blacklisted -- and the blacklist is reversible, so a user who
        dismisses something by mistake can un-blacklist it and let it
        come back.
        """
        conn, cur = self._connect()
        try:
            cur.execute(
                "DELETE FROM sql_recurring_pattern "
                "WHERE merchant_key = ? AND category = ? AND interval = ?",
                (merchant_key, category, interval),
            )
            cur.execute(
                "DELETE FROM sql_recurring_draft "
                "WHERE merchant_key = ? AND category = ? AND interval = ?",
                (merchant_key, category, interval),
            )
            cur.execute(
                "INSERT OR IGNORE INTO sql_recurring_blacklist "
                "(merchant_key, category, interval, blacklisted_at) VALUES (?, ?, ?, ?)",
                (merchant_key, category, interval, datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()
        return True, "Blacklisted recurring pattern."

    def unblacklist_recurring(self, merchant_key: str, category: str, interval: str) -> Tuple[bool, str]:
        """
        Reverse a blacklist entry. Always single-row/granular, and the
        triple becomes eligible for detection again on the next refresh.
        """
        conn, cur = self._connect()
        try:
            cur.execute(
                "DELETE FROM sql_recurring_blacklist "
                "WHERE merchant_key = ? AND category = ? AND interval = ?",
                (merchant_key, category, interval),
            )
            if cur.rowcount == 0:
                conn.rollback()
                return False, "Blacklist entry not found."
            conn.commit()
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()
        return True, "Un-blacklisted."

    def get_recurring_blacklist(self) -> List[Dict[str, Any]]:
        conn, cur = self._connect()
        try:
            cur.execute(
                "SELECT merchant_key, category, interval, blacklisted_at "
                "FROM sql_recurring_blacklist ORDER BY blacklisted_at DESC"
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        return [{"merchant_key": r[0], "category": r[1], "interval": r[2],
                 "blacklisted_at": r[3]} for r in rows]

    def get_recurring_drafts(self) -> List[Dict[str, Any]]:
        """
        Staged candidates awaiting review, most confident first.

        The confidence shown here is computed live from the stored
        evidence, exactly as it is for confirmed patterns -- a stale
        score is never returned to the user.
        """
        conn, cur = self._connect()
        try:
            cur.execute(
                "SELECT merchant_key, merchant_display, category, interval, expected_amount, "
                "amount_variance, expected_next_date, occurrences, interval_regularity, "
                "evidence_dates, created_at FROM sql_recurring_draft"
            )
            rows = cur.fetchall()
        finally:
            conn.close()

        results = []
        for (merchant_key, merchant_display, category, interval, expected_amount,
             amount_variance, expected_next_date, occurrences, regularity,
             evidence_json, created_at) in rows:
            stability = amount_stability(money(expected_amount), money(amount_variance))
            confidence = compute_confidence(regularity, stability, occurrences, "yellow")
            try:
                evidence = json.loads(evidence_json) if evidence_json else []
            except (TypeError, ValueError):
                evidence = []
            results.append({
                "merchant_key": merchant_key,
                "merchant_display": merchant_display,
                "category": category,
                "interval": interval,
                "expected_amount": money(expected_amount),
                "amount_variance": money(amount_variance),
                "expected_next_date": expected_next_date,
                "occurrences": occurrences,
                "interval_regularity": regularity,
                "amount_stability": round(stability, 4),
                "confidence": round(confidence, 4),
                "evidence_dates": evidence,
                "created_at": created_at,
            })
        results.sort(key=lambda r: r["confidence"], reverse=True)
        return results

    def get_recurring_patterns(self, min_confidence: Optional[float] = None) -> List[Dict[str, Any]]:
        """
        Live-computed view of confirmed recurring patterns -- never a
        cached score, exactly like get_merchant_patterns.

        Blacklisted triples are excluded entirely. Confidence is
        recomputed from the stored evidence (interval regularity, amount
        stability, occurrence count) and the user's rating on every
        call, so a Green vouch or a newly reconciled occurrence shows
        up immediately.
        """
        conn, cur = self._connect()
        try:
            cur.execute("SELECT merchant_key, category, interval FROM sql_recurring_blacklist")
            blacklisted = set(cur.fetchall())

            cur.execute(
                "SELECT merchant_key, merchant_display, category, interval, expected_amount, "
                "amount_variance, expected_next_date, occurrences, interval_regularity, "
                "rating, user_declared, source, first_seen_at, last_seen_at "
                "FROM sql_recurring_pattern"
            )
            raw_rows = cur.fetchall()
        finally:
            conn.close()

        raw_rows = [r for r in raw_rows if (r[0], r[2], r[3]) not in blacklisted]

        results = []
        for (merchant_key, merchant_display, category, interval, expected_amount,
             amount_variance, expected_next_date, occurrences, regularity, rating,
             user_declared, source, first_seen_at, last_seen_at) in raw_rows:
            expected_amount = money(expected_amount)
            amount_variance = money(amount_variance)
            if source == "user_declared":
                # A declared pattern has no transaction evidence to
                # weigh -- the declaration itself is the evidence.
                stability = 1.0
                confidence = compute_declared_confidence(rating)
            else:
                stability = amount_stability(expected_amount, amount_variance)
                confidence = compute_confidence(regularity, stability, occurrences, rating)
            results.append({
                "merchant_key": merchant_key,
                "merchant_display": merchant_display,
                "category": category,
                "interval": interval,
                "expected_amount": expected_amount,
                "amount_variance": amount_variance,
                "expected_next_date": expected_next_date,
                "occurrences": occurrences,
                "interval_regularity": regularity,
                "amount_stability": round(stability, 4),
                "confidence": round(confidence, 4),
                "rating": rating,
                "user_declared": bool(user_declared),
                "source": source,
                "first_seen_at": first_seen_at,
                "last_seen_at": last_seen_at,
                "can_promote": confidence >= PROMOTE_THRESHOLD,
            })

        if min_confidence is not None:
            results = [r for r in results if r["confidence"] >= min_confidence]
        results.sort(key=lambda r: r["confidence"], reverse=True)
        return results

    def declare_recurring_pattern(self, merchant: str, category: str, interval: str,
                                  amount, expected_next_date: Optional[str]) -> Tuple[bool, str]:
        """
        Record a recurring charge the user declares outright.

        The auto-detector needs a cadence to work with, so a
        subscription that started last month -- one or two payments, no
        pattern to find -- is invisible to it. A declaration is how the
        user says "I know this one recurs"; it is stored as a normal
        pattern with user_declared set, so consumers read it exactly
        like a detected one.

        Declared patterns are the user's own record: the detector
        refreshes detected patterns, but never overwrites a declared
        one.
        """
        if interval not in INTERVALS:
            return False, f"interval must be one of: {', '.join(INTERVALS)}"

        amount = money(amount)
        if amount <= 0:
            return False, "Amount must be greater than zero."

        merchant_key, merchant_display = normalize_merchant(merchant or "")
        if not merchant_key:
            return False, "Merchant could not be interpreted."

        conn, cur = self._connect()
        try:
            cur.execute(
                "SELECT 1 FROM sql_recurring_blacklist "
                "WHERE merchant_key = ? AND category = ? AND interval = ?",
                (merchant_key, category, interval),
            )
            if cur.fetchone():
                return False, "This pattern is blacklisted. Un-blacklist it first."

            now = datetime.now().isoformat(timespec="seconds")
            cur.execute(
                "INSERT INTO sql_recurring_pattern "
                "(merchant_key, merchant_display, category, interval, expected_amount, "
                " amount_variance, expected_next_date, occurrences, interval_regularity, "
                " rating, user_declared, source, first_seen_at, last_seen_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, '0.00', ?, 0, 1.0, 'green', 1, 'user_declared', ?, ?, ?, ?) "
                "ON CONFLICT(merchant_key, category, interval) DO UPDATE SET "
                "merchant_display = excluded.merchant_display, "
                "expected_amount = excluded.expected_amount, "
                "expected_next_date = excluded.expected_next_date, "
                "rating = excluded.rating, "
                "user_declared = excluded.user_declared, "
                "source = excluded.source, "
                "updated_at = excluded.updated_at",
                (merchant_key, merchant_display, category, interval, str(amount),
                 expected_next_date, now, now, now, now),
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()
        return True, "Recurring pattern declared."

    def business_income_total(self, start_date: str, end_date: str) -> Decimal:
        """Sum total net income/revenue across all Income accounts within the date range."""
        total = ZERO
        for tx in self.transactions:
            if not (start_date <= tx.date <= end_date):
                continue
            for line in tx.lines:
                acc = self.accounts.get(line.account_name)
                if acc and acc.account_type in ("Income", "Revenue"):
                    total += (line.cr - line.dr)
        return money(total)

    def medical_expenses_total(self, start_date: str, end_date: str, medical_accounts: list = None) -> Decimal:
        """Sum total debits for specified medical/dental expense accounts within the date range."""
        total = ZERO
        medical_accounts = set(medical_accounts) if medical_accounts else set()
        for tx in self.transactions:
            if not (start_date <= tx.date <= end_date):
                continue
            for line in tx.lines:
                if medical_accounts and line.account_name not in medical_accounts:
                    continue
                acc = self.accounts.get(line.account_name)
                if acc and acc.account_type == "Expense" and line.dr > ZERO:
                    total += line.dr
        return money(total)

    def get_business_expense_transactions(
        self, start_date: str, end_date: str, category_map: Dict[str, List[str]]
    ) -> List[Transaction]:
        """Fetch transactions containing at least one eligible business expense account defined in category_map."""
        eligible_accounts = set()
        for accounts in category_map.values():
            if isinstance(accounts, str):
                eligible_accounts.add(accounts)
            else:
                eligible_accounts.update(accounts)

        txs = self.get_transactions(start_date=start_date, end_date=end_date)
        result = []
        for tx in txs:
            for line in tx.lines:
                acc = self.accounts.get(line.account_name)
                if acc and acc.account_type == "Expense" and line.dr > ZERO:
                    if line.account_name in eligible_accounts:
                        result.append(tx)
                        break
        return result

    def aggregate_by_category(
        self, start_date: str, end_date: str, category_map: Dict[str, Any]
    ) -> Dict[str, Decimal]:
        """
        Sum net activity for every account in a category map, preserving
        the account's normal-balance sign. Revenue accounts contribute
        credits-minus-debits; Expense accounts contribute debits-minus-
        credits. Both are therefore POSITIVE in the normal case, so a
        category mixing a Revenue account with an Expense account ADDS
        them. To make an account subtract (e.g. realized losses netting
        against realized gains), give its category_map entry as
        {"account": "<name>", "sign": -1}; "sign" defaults to 1 and
        must be 1 or -1.
        """
        if not category_map:
            return {}

        # Build a reverse lookup: account_name -> category_name
        account_to_category = {}
        account_sign = {}
        for cat, accounts in category_map.items():
            if isinstance(accounts, str):
                accounts = [accounts]
            for entry in accounts:
                if isinstance(entry, str):
                    account_to_category[entry] = cat
                elif isinstance(entry, dict):
                    acct = entry.get("account")
                    if acct:
                        account_to_category[acct] = cat
                        sign = entry.get("sign", 1)
                        if isinstance(sign, bool) or sign not in (1, -1):
                            raise ValueError(
                                f"category_map entry for account '{acct}' has sign {sign!r}; "
                                f"it must be 1 or -1."
                            )
                        account_sign[acct] = sign

        totals = {cat: ZERO for cat in category_map.keys()}

        # Get all transactions in the date range
        txs = self.get_transactions(start_date=start_date, end_date=end_date)

        for tx in txs:
            for line in tx.lines:
                if line.account_name not in account_to_category:
                    continue

                acc = self.accounts.get(line.account_name)
                if not acc:
                    continue

                # Determine normal balance sign per the brief:
                # DR normal (Asset, Expense): dr - cr
                # CR normal (Liability, Equity, Revenue): cr - dr
                if acc.NORMAL_BALANCES[acc.account_type] == "DR":
                    net_activity = line.dr - line.cr
                else:
                    net_activity = line.cr - line.dr

                cat = account_to_category[line.account_name]
                totals[cat] += net_activity * account_sign.get(line.account_name, 1)

        return {k: money(v) for k, v in totals.items()}

    def expenses_by_category(
        self, start_date: str, end_date: str, category_map: Dict[str, List[str]]
    ) -> Dict[str, Decimal]:
        """
        Thin wrapper around aggregate_by_category, strictly filtered to
        Expense accounts to preserve legacy behavior for XLSX exports
        and older UI components.
        """
        if not category_map:
            return {}

        # Filter the map to only include actual Expense accounts
        expense_map = {}
        for cat, accounts in category_map.items():
            if isinstance(accounts, str):
                accounts = [accounts]
            exp_accs = [
                acc_name for acc_name in accounts
                if acc_name in self.accounts and self.accounts[acc_name].account_type == "Expense"
            ]
            if exp_accs:
                expense_map[cat] = exp_accs

        return self.aggregate_by_category(start_date, end_date, expense_map)

    def medical_expenses_total(
        self, start_date: str, end_date: str, medical_accounts: List[str]
    ) -> Decimal:
        """Net medical spending (debits minus credits), so a voided or
        refunded medical expense no longer counts toward the total."""
        if not medical_accounts:
            return ZERO
        total = ZERO
        txs = self.get_transactions(start_date=start_date, end_date=end_date)
        for tx in txs:
            for line in tx.lines:
                acc = self.accounts.get(line.account_name)
                if acc and acc.account_type == "Expense":
                    if line.account_name in medical_accounts:
                        total += line.dr - line.cr
        return money(total)

    def export_tax_report_xlsx(
        self,
        start_date: str,
        end_date: str,
        path: str,
        category_map: Dict[str, List[str]],
        include_medical: bool = True,
        medical_accounts: Optional[List[str]] = None,
    ):
        import pandas as pd

        if medical_accounts is None:
            medical_accounts = []

        income = self.business_income_total(start_date, end_date)
        expenses_by_cat = self.expenses_by_category(start_date, end_date, category_map)
        total_expenses = sum(expenses_by_cat.values(), ZERO)
        net = income - total_expenses

        df_summary = pd.DataFrame([
            {"Metric": "Period Start", "Value": start_date},
            {"Metric": "Period End", "Value": end_date},
            {"Metric": "Gross Business Income", "Value": income},
            {"Metric": "Total Expenses", "Value": total_expenses},
            {"Metric": "Net Business Income (Loss)", "Value": net},
        ])
        df_exp = pd.DataFrame([
            {"Category": cat, "Amount": amt}
            for cat, amt in expenses_by_cat.items()
            if amt != ZERO
        ])

        txs = self.get_business_expense_transactions(start_date, end_date, category_map)
        biz_rows = []
        eligible_accounts = {
            acc
            for accs in category_map.values()
            for acc in (accs if isinstance(accs, list) else [accs])
        }
        for tx in txs:
            for line in tx.lines:
                acc = self.accounts.get(line.account_name)
                if acc and acc.account_type == "Expense" and line.dr > ZERO:
                    if line.account_name in eligible_accounts:
                        biz_rows.append({
                            "Date": tx.date,
                            "Description": tx.description,
                            "Account": line.account_name,
                            "Amount": line.dr,
                        })
        df_biz = pd.DataFrame(biz_rows)

        df_med = None
        if include_medical and medical_accounts:
            med_rows = []
            med_txs = self.get_transactions(start_date=start_date, end_date=end_date)
            for tx in med_txs:
                for line in tx.lines:
                    acc = self.accounts.get(line.account_name)
                    if acc and acc.account_type == "Expense" and line.dr > ZERO:
                        if line.account_name in medical_accounts:
                            med_rows.append({
                                "Date": tx.date,
                                "Description": tx.description,
                                "Account": line.account_name,
                                "Amount": line.dr,
                            })
            df_med = pd.DataFrame(med_rows) if med_rows else None

        for df in [df_summary, df_exp, df_biz, df_med]:
            if df is not None and not df.empty:
                for col in df.columns:
                    # Only apply to string/object columns
                    if df[col].dtype == object:
                        df[col] = df[col].apply(_sanitize_for_excel)


        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            df_summary.to_excel(writer, sheet_name="Summary", index=False)
            df_exp.to_excel(writer, sheet_name="Expenses_by_Category", index=False)
            df_biz.to_excel(writer, sheet_name="Business_Transactions", index=False)
            if include_medical and df_med is not None:
                df_med.to_excel(writer, sheet_name="Medical_Expenses", index=False)

    def get_account_totals(self, account_name: str, start_date: str, end_date: str) -> Tuple[Decimal, Decimal]:
        """
        Returns (total_debits, total_credits) for a specific account
        within a date range. Used by tax templates to calculate totals.
        """
        total_dr = ZERO
        total_cr = ZERO
        for tx in self.get_transactions(start_date=start_date, end_date=end_date, account=account_name):
            for line in tx.lines:
                if line.account_name == account_name:
                    total_dr += line.dr
                    total_cr += line.cr
        return money(total_dr), money(total_cr)

    def post_simple_transaction(self, date: str, description: str, credit_account: str, debit_account: str, amount: Any) -> Tuple[bool, str]:
        if credit_account not in self.accounts or debit_account not in self.accounts:
            return False, "One or both accounts do not exist."

        if debit_account == credit_account:
            return False, "Debit and credit accounts must be different."

        m_amount = money(amount)
        return self.record_transaction(
            date,
            description,
            [
                (credit_account, ZERO, m_amount),
                (debit_account, m_amount, ZERO)
            ],
            persist=True
        )

    def transfer_funds(self, date: str, description: str, from_account: str, to_account: str, amount: Any) -> Tuple[bool, str]:
        return self.post_simple_transaction(
            date=date,
            description=description,
            credit_account=from_account,
            debit_account=to_account,
            amount=amount
        )

    def _investment_crypto_lock_message(self, tx_id: int) -> Optional[str]:
        """Investment/crypto modules keep their own per-holding lot
        tables (quantity, cost basis) in sync with specific ledger
        transactions -- see investment_engine.py / crypto_engine.py.
        Editing or voiding one of those transactions through these
        generic methods would silently desync quantity/cost-basis
        tracking from the dollar amounts posted here, with no error to
        signal it happened. If either module's log table exists and
        references this tx_id, redirect the caller to that module's
        own void_last_transaction() instead, which reverses both sides
        together. Fully decoupled from those modules: if investment/
        crypto features have never been used, their log tables don't
        exist and this is a no-op (caught, not raised).
        """
        conn, cur = self._connect()
        try:
            for table in ("sql_investment_tx_log", "sql_crypto_tx_log"):
                try:
                    cur.execute(f"SELECT 1 FROM {table} WHERE tx_id = ? LIMIT 1", (tx_id,))
                    if cur.fetchone():
                        return (
                            "This transaction was created by the Investments/Crypto module. "
                            "Editing or voiding it here would desync it from quantity/cost-basis "
                            "tracking. Use 'Void Most Recent Transaction' on the holding in the "
                            "Investments & Crypto tab instead."
                        )
                except Exception:
                    continue  # table doesn't exist yet -- feature unused, nothing to guard
            return None
        finally:
            conn.close()

    def void_transaction(self, tx_id: int) -> Tuple[bool, str]:
        """
        APPEND-ONLY DELETE: Voids the original transaction by creating a
        perfect reversal (flipping DR/CR) and marking the original as voided.
        """

        guard_msg = self._investment_crypto_lock_message(tx_id)
        if guard_msg:
            return False, guard_msg
        conn, cur = self._connect()
        try:
            # 1. Fetch original state
            cur.execute("SELECT date, description, reconciled FROM sql_transactions WHERE id = ? AND is_voided = 0", (tx_id,))
            row = cur.fetchone()
            if not row:
                return False, "Transaction not found or already voided."
            old_date, old_desc, reconciled = row

            cur.execute("SELECT DISTINCT account_name FROM sql_transaction_lines WHERE tx_id = ?", (tx_id,))
            lock_msg = self.period_lock_violation(old_date, [r[0] for r in cur.fetchall()])
            if lock_msg:
                return False, lock_msg

            if reconciled:
                return False, "Cannot void a reconciled transaction. Unreconcile it first."

            cur.execute("SELECT id, account_name, dr, cr FROM sql_transaction_lines WHERE tx_id = ?", (tx_id,))
            old_lines = [(lid, acc, money(dr) if dr else ZERO, money(cr) if cr else ZERO) for lid, acc, dr, cr in cur.fetchall()]

            # 2. Create the reversal transaction (flip DR and CR)
            reversal_lines = [(None, acc, cr, dr) for _, acc, dr, cr in old_lines]
            reversal_desc = f"VOID: {old_desc}"

            cur.execute(
                "INSERT INTO sql_transactions (date, description, imported_id, reconciled, is_voided, reverses_tx_id) "
                "VALUES (?, ?, NULL, 0, 0, ?)",
                (old_date, reversal_desc, tx_id)
            )
            new_tx_id = cur.lastrowid

            for _, acc, dr, cr in reversal_lines:
                cur.execute(
                    "INSERT INTO sql_transaction_lines (tx_id, account_name, dr, cr) VALUES (?, ?, ?, ?)",
                    (new_tx_id, acc, str(dr), str(cr))
                )

            # 3. Mark original as voided
            cur.execute("UPDATE sql_transactions SET is_voided = 1, reversed_by_tx_id = ? WHERE id = ?", (new_tx_id, tx_id))

            # 4. Log to history (The chain remains unbroken)
            self._log_history(cur, tx_id, "voided", old_date, old_desc, old_lines)
            self._log_history(cur, new_tx_id, "created", old_date, reversal_desc, reversal_lines)

            conn.commit()
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()

        self._load_from_db()
        return True, "Transaction voided and reversed."

    def edit_transaction(self, tx_id: int, new_date: str, new_description: str, new_lines: List[Tuple[Optional[int], str, Any, Any]], reason: Optional[str] = None) -> Tuple[bool, str]:
        """
        APPEND-ONLY EDIT: Voids the original transaction and creates a brand new
        one with the updated details. Preserves the immutable audit trail.
        """
        guard_msg = self._investment_crypto_lock_message(tx_id)
        if guard_msg:
            return False, guard_msg

        # 1. Validate new lines (same logic as before)
        if len(new_lines) < 2:
            return False, "A transaction must contain at least two lines."

        parsed = []
        try:
            for line_id, acc, dr, cr in new_lines:
                dr_val, cr_val = money(dr), money(cr)
                if not dr_val.is_finite() or not cr_val.is_finite():
                    raise ValueError(f"non-finite amount on account '{acc}'")
                parsed.append((acc, dr_val, cr_val)) # We strip line_id for the new insert
        except (InvalidOperation, ValueError, TypeError) as e:
            return False, f"Invalid amount: {e}"

        for acc, dr, cr in parsed:
            if dr < ZERO or cr < ZERO: return False, "Negative amounts not allowed."
            if dr > ZERO and cr > ZERO: return False, "Line cannot have both DR and CR."
            if dr == ZERO and cr == ZERO: return False, "Line must have DR or CR."
            if acc not in self.accounts: return False, f"Account '{acc}' does not exist."

        total_dr = sum((dr for _, dr, _ in parsed), ZERO)
        total_cr = sum((cr for _, _, cr in parsed), ZERO)
        if total_dr != total_cr:
            return False, "Transaction out of balance."

        lock_msg = self.period_lock_violation(new_date, [acc for acc, _, _ in parsed])
        if lock_msg:
            return False, lock_msg

        conn, cur = self._connect()
        try:
            # 2. Fetch original state
            cur.execute("SELECT date, description, reconciled, imported_id  FROM sql_transactions WHERE id = ? AND is_voided = 0", (tx_id,))
            row = cur.fetchone()
            if not row:
                return False, "Transaction not found or already voided."
            old_date, old_desc, reconciled, old_imp_id = row

            cur.execute("SELECT DISTINCT account_name FROM sql_transaction_lines WHERE tx_id = ?", (tx_id,))
            lock_msg = self.period_lock_violation(old_date, [r[0] for r in cur.fetchall()])
            if lock_msg:
                return False, lock_msg

            if reconciled:
                return False, "Cannot edit a reconciled transaction. Unreconcile it first."

            cur.execute("SELECT id, account_name, dr, cr FROM sql_transaction_lines WHERE tx_id = ?", (tx_id,))
            old_lines = [(lid, acc, money(dr) if dr else ZERO, money(cr) if cr else ZERO) for lid, acc, dr, cr in cur.fetchall()]

            # 3. Reverse the original with an explicit, fully-counted
            # entry -- the ledger never excludes a posted transaction
            # from balances (see _load_from_db()). is_voided=1 is set
            # on the original purely as a display/audit marker now, not
            # a balance gate -- without this explicit reversal, the
            # original's dollar effect would stay fully counted AND the
            # new corrected transaction below would also count,
            # double-posting the change.
            reversal_lines = [(acc, cr, dr) for _, acc, dr, cr in old_lines]
            cur.execute(
                "INSERT INTO sql_transactions (date, description, imported_id, reconciled, is_voided, reverses_tx_id, change_reason) "
                "VALUES (?, ?, NULL, 0, 0, ?, ?)",
                (old_date, f"Reversal (edited): {old_desc}", tx_id, reason)
            )
            reversal_tx_id = cur.lastrowid
            for acc, dr, cr in reversal_lines:
                cur.execute(
                    "INSERT INTO sql_transaction_lines (tx_id, account_name, dr, cr) VALUES (?, ?, ?, ?)",
                    (reversal_tx_id, acc, str(dr), str(cr))
                )
            cur.execute("UPDATE sql_transactions SET is_voided = 1, reversed_by_tx_id = ? WHERE id = ?", (reversal_tx_id, tx_id))

            # 4. Insert new transaction (the corrected version)
            cur.execute(
                "INSERT INTO sql_transactions (date, description, imported_id, reconciled, is_voided, reverses_tx_id, change_reason) "
                "VALUES (?, ?, ?, 0, 0, ?, ?)",
                (new_date, new_description, old_imp_id, tx_id, reason)
            )
            new_tx_id = cur.lastrowid

            for acc, dr, cr in parsed:
                cur.execute(
                    "INSERT INTO sql_transaction_lines (tx_id, account_name, dr, cr) VALUES (?, ?, ?, ?)",
                    (new_tx_id, acc, str(dr), str(cr))
                )

            # 5. Log to history
            self._log_history(cur, tx_id, "voided", old_date, old_desc, old_lines)
            self._log_history(cur, reversal_tx_id, "created", old_date, f"Reversal (edited): {old_desc}",
                               [(None, acc, dr, cr) for acc, dr, cr in reversal_lines])
            self._log_history(cur, new_tx_id, "created", new_date, new_description, [(None, acc, dr, cr) for acc, dr, cr in parsed], change_reason=reason)

            conn.commit()
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()

        self._load_from_db()
        return True, "Transaction edited (original voided, new created)."

    def verify_audit_chain_integrity(self) -> Tuple[bool, str]:
        """
        Verifies the cryptographic hash chain of the audit trail.
        Returns (is_valid, error_message).
        """
        conn, cur = self._connect()
        try:
            cur.execute(
                "SELECT id, tx_id, change_type, changed_at, date, description, lines_json, change_reason, prev_hash, row_hash "
                "FROM sql_transaction_history ORDER BY id ASC"
            )
            rows = cur.fetchall()

            expected_prev_hash = "0" * 64
            for row in rows:
                hid, tx_id, change_type, changed_at, date, description, lines_json, change_reason, stored_prev_hash, stored_row_hash = row

                if stored_prev_hash != expected_prev_hash:
                    return False, f"Chain broken at history ID {hid}: expected prev_hash {expected_prev_hash[:16]}..., got {stored_prev_hash[:16]}..."

                # Recompute the hash
                payload = f"{tx_id}{change_type}{changed_at}{date}{description}{lines_json}{change_reason or ''}{stored_prev_hash}"
                computed_hash = hashlib.sha256(payload.encode('utf-8')).hexdigest()

                if computed_hash != stored_row_hash:
                    return False, f"Tampering detected at history ID {hid}: row hash mismatch."

                expected_prev_hash = stored_row_hash

            return True, f"Audit chain verified. {len(rows)} entries intact."
        finally:
            conn.close()

    def reassign_account(
        self,
        old_account: str,
        new_account: str,
        imported_only: bool = True,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        Bulk-move every transaction LINE currently posted to `old_account`
        over to `new_account`. Relabels the account only -- debit/credit
        values are left exactly as they were.

        Built for the "QFX import landed everything in the wrong account"
        case: editing hundreds of misfiled transactions one at a time via
        update_transaction() is impractical, and there was previously no
        way to fix a bad import short of deleting it entirely.

        IMPORTANT -- this does NOT and should NOT try to "fix" debit/credit
        direction. An earlier version of this method had a flip_dr_cr
        option; it's been removed after testing showed it corrupts the
        ledger. Flipping one line's dr/cr without symmetrically adjusting
        the transaction's OTHER line breaks the balance invariant outright
        (total dr must equal total cr WITHIN each transaction, not just
        across the whole ledger). Beyond that bug, whether a line's
        direction is even "wrong" depends on whether that specific
        transaction was a deposit or a withdrawal -- a blanket batch-level
        flip can't know that, and would get roughly half of any mixed
        batch backwards.

        If some of the misfiled transactions also have the wrong
        debit/credit direction (check this against the real statement --
        an account should be debited when money entered it and credited
        when money left, regardless of the account's type label), fix
        those specific transactions individually with update_transaction(),
        which validates balance on every call. Use THIS method only to
        correct the account label in bulk; use that one for anything
        needing a direction correction.

        imported_only=True (the default) restricts this to lines whose
        parent transaction has a non-null imported_id, so a bad import
        can be corrected without also touching manually-entered
        transactions that happen to share the same (correct) account
        name. Pass False to widen it if you actually want everything.

        date_from/date_to (inclusive, "YYYY-MM-DD") let you scope to a
        specific statement's date range if imported_only alone isn't
        precise enough.

        Every affected transaction gets a normal "updated" history entry
        (old state logged before the change), exactly like a manual edit
        through update_transaction() would -- this is a bulk *edit*, not
        a special bypass of the audit trail.
        """
        if new_account not in self.accounts:
            return False, f"Account '{new_account}' does not exist."
        if old_account == new_account:
            return False, "Old and new account are the same; nothing to do."

        conn, cur = self._connect()
        try:
            query = (
                "SELECT DISTINCT stl.tx_id FROM sql_transaction_lines stl "
                "JOIN sql_transactions st ON st.id = stl.tx_id "
                "WHERE stl.account_name = ?"
            )
            params: list = [old_account]
            if imported_only:
                query += " AND st.imported_id IS NOT NULL"
            if date_from:
                query += " AND st.date >= ?"
                params.append(date_from)
            if date_to:
                query += " AND st.date <= ?"
                params.append(date_to)

            cur.execute(query, params)
            tx_ids = [row[0] for row in cur.fetchall()]

            if not tx_ids:
                return True, f"No matching transactions found on '{old_account}'; nothing changed."

            # Locked periods: bulk relabelling is all-or-nothing, so check
            # every affected transaction (old accounts AND the new one)
            # before logging or changing anything.
            blocked = 0
            for tx_id in tx_ids:
                snap = self._fetch_transaction_snapshot(cur, tx_id)
                if snap is None:
                    continue
                snap_date, _, snap_lines = snap
                accs = {acc for _, acc, _, _ in snap_lines} | {new_account}
                if self.period_lock_violation(snap_date, accs):
                    blocked += 1
            if blocked:
                return False, (
                    f"{blocked} of the {len(tx_ids)} matching transaction(s) fall in a locked "
                    "period; nothing was changed. Narrow the date range or reopen the period."
                )

            for tx_id in tx_ids:
                snapshot = self._fetch_transaction_snapshot(cur, tx_id)
                if snapshot is None:
                    continue  # shouldn't happen given the JOIN above, but stay defensive
                old_date, old_description, old_lines = snapshot
                self._log_history(cur, tx_id, "updated", old_date, old_description, old_lines)

            id_placeholders = ",".join("?" for _ in tx_ids)
            cur.execute(
                "UPDATE sql_transaction_lines SET account_name = ? "
                "WHERE account_name = ? AND tx_id IN ({})".format(id_placeholders),
                [new_account, old_account] + tx_ids,
            )
            affected_lines = cur.rowcount
            conn.commit()
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()

        self._load_from_db()
        return True, (
            f"Reassigned {affected_lines} line(s) across {len(tx_ids)} transaction(s) "
            f"from '{old_account}' to '{new_account}'."
        )

    def income_statement_data(self, start_date: str, end_date: str) -> Dict[str, Any]:
        """
        Raw account activity grouped for the income statement builder.

        Each group is a list of (account_name, signed_amount). Revenue is
        positive (credit-normal), expenses are stored as negative values
        (their debit balance, negated) so the builder's arithmetic is
        plain addition.

        Account grouping derives from the account's `report_line` and its
        subcategory via REPORT_LINE_TO_SUBCATEGORY. Accounts with no
        report line land in "other_unclassified" for expenses and
        "other_revenue" for revenue.
        """
        # Validate dates
        try:
            datetime.strptime(start_date, "%Y-%m-%d")
            datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            raise ValueError("Invalid date format (use YYYY-MM-DD).")

        # Initialize all groups
        groups: Dict[str, List[Tuple[str, Decimal]]] = {
            "operating_revenue": [],
            "non_operating_revenue": [],
            "other_revenue": [],
            "cogs": [],
            "operating_expenses": [],
            "depreciation": [],
            "interest_and_bank": [],
            "income_tax": [],
            "other_expenses": [],
            "other_unclassified": [],
            "investment_income": [],
            "investment_losses": [],
        }

        # Accumulate per account
        account_totals: Dict[str, Decimal] = {}

        txs = self.get_transactions(start_date=start_date, end_date=end_date)
        for tx in txs:
            for line in tx.lines:
                acc = self.accounts.get(line.account_name)
                if acc is None:
                    continue
                if acc.account_type == "Revenue":
                    amt = line.cr - line.dr
                elif acc.account_type == "Expense":
                    # Store as negative (expense reduces net income)
                    amt = -(line.dr - line.cr)
                else:
                    continue
                account_totals[line.account_name] = account_totals.get(
                    line.account_name, ZERO
                ) + amt

        # Assign each account to a group
        for name, amt in account_totals.items():
            if amt == ZERO:
                continue
            acc = self.accounts[name]
            sub = acc.subcategory  # None if no report_line set

            if acc.account_type == "Revenue":
                if sub == "Operating Revenue":
                    groups["operating_revenue"].append((name, money(amt)))
                elif sub == "Non-Operating Revenue":
                    groups["non_operating_revenue"].append((name, money(amt)))
                elif sub in ("Investment Income", "Crypto Income"):
                    groups["investment_income"].append((name, money(amt)))
                else:
                    # Includes "Other Income" subcategory AND unclassified
                    # revenue accounts -- both belong under Other Income on
                    # the statement.
                    groups["other_revenue"].append((name, money(amt)))
            else:  # Expense
                if sub == "Cost of Goods Sold":
                    groups["cogs"].append((name, money(amt)))
                elif sub == "Operating Expenses":
                    groups["operating_expenses"].append((name, money(amt)))
                elif sub == "Depreciation & Amortization":
                    groups["depreciation"].append((name, money(amt)))
                elif sub == "Interest & Bank Charges":
                    groups["interest_and_bank"].append((name, money(amt)))
                elif sub == "Income Tax":
                    groups["income_tax"].append((name, money(amt)))
                elif sub in ("Investment Losses", "Crypto Losses"):
                    groups["investment_losses"].append((name, money(amt)))
                else:
                    # No report line, or one that isn't a specific P&L
                    # subcategory -- visibly flagged as unclassified so the
                    # user knows to classify the account.
                    groups["other_unclassified"].append((name, money(amt)))

        # Sort each group alphabetically
        for key in groups:
            groups[key].sort(key=lambda t: t[0])

        # Compute totals
        def group_total(key: str) -> Decimal:
            return money(sum((amt for _, amt in groups[key]), ZERO))

        total_revenue = (
            group_total("operating_revenue")
            + group_total("non_operating_revenue")
            + group_total("other_revenue")
        )
        total_cogs = group_total("cogs")
        gross_profit = money(total_revenue + total_cogs)
        total_opex = group_total("operating_expenses")
        operating_income = money(gross_profit + total_opex)
        total_other = (
            group_total("other_expenses")
            + group_total("depreciation")
            + group_total("interest_and_bank")
            + group_total("income_tax")
            + group_total("other_unclassified")
            + group_total("investment_income")
            + group_total("investment_losses")
        )
        net_income = money(operating_income + total_other)

        totals = {
            "total_revenue": total_revenue,
            "total_cogs": total_cogs,
            "gross_profit": gross_profit,
            "total_operating_expenses": total_opex,
            "operating_income": operating_income,
            "total_other": total_other,
            "net_income": net_income,
        }

        return {"groups": groups, "totals": totals}

    def monthly_income_expense_report(self, year: int, month: int) -> Dict[str, Any]:
        """Income and expenses for a month, netted the way each account type
        normally balances: Revenue credits minus debits, Expense debits minus
        credits. A refund, a void and the reversal an edit posts therefore all
        reduce the figure they belong to, and nothing is counted twice.
        Accounts that net to exactly zero are left out of the by-account maps.
        """
        start = f"{year:04d}-{month:02d}-01"
        last_day = monthrange(year, month)[1]
        end = f"{year:04d}-{month:02d}-{last_day:02d}"
        income_by_account: Dict[str, Decimal] = {}
        expense_by_account: Dict[str, Decimal] = {}
        txs = self.get_transactions(start_date=start, end_date=end)
        for tx in txs:
            for line in tx.lines:
                acc = self.accounts[line.account_name]
                if acc.account_type == "Revenue":
                    income_by_account[line.account_name] = income_by_account.get(line.account_name, ZERO) + line.cr - line.dr
                elif acc.account_type == "Expense":
                    expense_by_account[line.account_name] = expense_by_account.get(line.account_name, ZERO) + line.dr - line.cr
        income_total = sum(income_by_account.values(), ZERO)
        expense_total = sum(expense_by_account.values(), ZERO)
        return {
            "income_total": money(income_total),
            "expense_total": money(expense_total),
            "net": money(income_total - expense_total),
            "income_by_account": {k: money(v) for k, v in income_by_account.items() if money(v) != ZERO},
            "expense_by_account": {k: money(v) for k, v in expense_by_account.items() if money(v) != ZERO}
        }

    def export_monthly_income_expense_xlsx(self, year: int, month: int, path: str):
        import pandas as pd
        data = self.monthly_income_expense_report(year, month)
        df_summary = pd.DataFrame([
            {"Metric": "Year", "Value": year},
            {"Metric": "Month", "Value": month},
            {"Metric": "Total Income", "Value": data["income_total"]},
            {"Metric": "Total Expenses", "Value": data["expense_total"]},
            {"Metric": "Net (Income − Expenses)", "Value": data["net"]},
        ])
        df_inc = pd.DataFrame([{"Account": acc, "Amount": amt} for acc, amt in data["income_by_account"].items()])
        df_exp = pd.DataFrame([{"Account": acc, "Amount": amt} for acc, amt in data["expense_by_account"].items()])
        start = f"{year:04d}-{month:02d}-01"
        last_day = monthrange(year, month)[1]
        end = f"{year:04d}-{month:02d}-{last_day:02d}"
        txs = self.get_transactions(start_date=start, end_date=end)
        tx_rows = []
        for tx in txs:
            for line in tx.lines:
                acc = self.accounts[line.account_name]
                if acc.account_type not in ("Revenue", "Expense"):
                    continue
                amount = line.cr - line.dr if acc.account_type == "Revenue" else line.dr - line.cr
                tx_rows.append({"Date": tx.date, "Description": tx.description, "Account": line.account_name, "Type": acc.account_type, "Amount": amount})
        df_tx = pd.DataFrame(tx_rows)
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            df_summary.to_excel(writer, sheet_name="Summary", index=False)
            df_inc.to_excel(writer, sheet_name="Income_by_Account", index=False)
            df_exp.to_excel(writer, sheet_name="Expenses_by_Account", index=False)
            df_tx.to_excel(writer, sheet_name="Transactions", index=False)

    def close(self) -> None:
        """
        Explicit "I'm done with this ledger" hook for callers (main.py's
        first-run setup, ui_main.py's closeEvent).

        GeneralLedger opens a short-lived connection per operation via
        _connect() rather than holding one open for the object's
        lifetime, so there's no persistent connection to release here.
        What still matters on shutdown is durability: flush the WAL into
        the main .db file so the data on disk is fully up to date (and,
        as a safety net, roll back any sync batch left dangling by an
        unclean previous exit) -- both of which checkpoint() already
        does.
        """
        try:
            self.checkpoint()
        except Exception as e:
            logger.warning(f"GeneralLedger.close(): checkpoint failed: {e}")

    def checkpoint(self) -> None:
        """
        Force SQLite to flush the WAL (Write-Ahead Log) into the main
        database file. This guarantees the main .db file is 100%
        up-to-date before operations like backups that read the file
        directly.
        """
        # Safety net: If a sync batch is somehow dangling (e.g. the app
        # was closed mid-sync), roll it back so it doesn't block the
        # checkpoint or cause locking issues.
        if self._active_transaction_conn is not None:
            try:
                self.rollback_transaction()
            except Exception:
                pass

        conn, cur = self._connect()
        try:
            # Check if the database is actually in WAL mode
            cur.execute("PRAGMA journal_mode")
            mode = cur.fetchone()[0]

            if mode.lower() == "wal":
                # TRUNCATE forces a full checkpoint and then truncates
                # the WAL file to 0 bytes, ensuring the main DB file
                # is completely self-contained.
                cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()

    def create_encrypted_backup(self, backup_path: Optional[str] = None) -> Tuple[bool, str]:

        if backup_path is None:
            default_dir = str(BACKUP_DIR)
            os.makedirs(default_dir, exist_ok=True)
            backup_path = os.path.join(default_dir, "ledger_storage_backup.db")

        backup_dir = os.path.dirname(os.path.abspath(backup_path))
        os.makedirs(backup_dir, exist_ok=True)

        if os.path.abspath(backup_path) == os.path.abspath(self.db_path):
            return False, "Backup path cannot be the same as the main data"


        conn, cur = self._connect()
        try:
            if os.path.exists(backup_path):
                os.remove(backup_path)
            escaped_path = backup_path.replace("'", "''")
            cur.execute(f"ATTACH DATABASE '{escaped_path}' AS backup KEY \"x'{self.dek_hex}'\"")
            cur.execute("SELECT sqlcipher_export('backup')")
            cur.execute("DETACH DATABASE backup")
            conn.commit()
            self._harden_file_permissions(backup_path)
            return True, f"Encrypted backup created at {backup_path}"
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()

    # ---------------- Transaction History ----------------

    @staticmethod
    def _history_row_to_dict(row) -> Dict[str, Any]:
        hid, tx_id, change_type, changed_at, date, description, lines_json = row
        return {
            "id": hid,
            "tx_id": tx_id,
            "change_type": change_type,
            "changed_at": changed_at,
            "date": date,
            "description": description,
            "lines": json.loads(lines_json) if lines_json else [],
        }

    def get_transaction_history(self, tx_id: int) -> List[Dict[str, Any]]:
        """
        Return the full change history for a single transaction, oldest
        first: one entry for its creation, and one per subsequent edit
        (or a final one if it was deleted). Each entry is a snapshot of
        what the transaction looked like *before* that change was made
        (or, for "created", how it was first entered).
        """
        conn, cur = self._connect()
        try:
            cur.execute(
                "SELECT id, tx_id, change_type, changed_at, date, description, lines_json "
                "FROM sql_transaction_history WHERE tx_id = ? ORDER BY id",
                (tx_id,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        return [self._history_row_to_dict(r) for r in rows]

    def get_recent_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return the most recent transaction changes across the whole ledger (audit-log view)."""
        conn, cur = self._connect()
        try:
            cur.execute(
                "SELECT id, tx_id, change_type, changed_at, date, description, lines_json "
                "FROM sql_transaction_history ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        return [self._history_row_to_dict(r) for r in rows]

    def cleared_balance_as_of(self, account_name: str, as_of_date: str) -> Decimal:
        """
        The ledger's balance for an account considering ONLY transactions
        the user has marked reconciled, dated on or before as_of_date.
        Used for the live 'cleared so far' figure during an interactive
        reconciliation session -- distinct from running_balance_as_of(),
        which includes every transaction regardless of reconciled status
        and backs the separate, automated reconciliation_report() check.
        """
        if account_name not in self.accounts:
            raise ValueError(f"Account '{account_name}' does not exist.")
        acc = self.accounts[account_name]
        total_dr = ZERO
        total_cr = ZERO
        for tx in self.transactions:
            if not tx.reconciled or tx.date > as_of_date:
                continue
            for line in tx.lines:
                if line.account_name == account_name:
                    total_dr += line.dr
                    total_cr += line.cr
        bal = total_dr - total_cr if acc.NORMAL_BALANCES[acc.account_type] == "DR" else total_cr - total_dr
        return money(bal)

    def get_transactions_for_account(self, account_name: str, start_date: Optional[str] = None, end_date: Optional[str] = None) -> List[Transaction]:
        """
        All (non-voided) transactions touching a given account, optionally
        bounded by date range, regardless of reconciled state -- used by
        the reconciliation tab to show a full statement window rather than
        only never-reconciled items.
        """
        if account_name not in self.accounts:
            raise ValueError(f"Account '{account_name}' does not exist.")
        results = []
        for tx in self.transactions:
            if start_date and tx.date < start_date:
                continue
            if end_date and tx.date > end_date:
                continue
            if any(line.account_name == account_name for line in tx.lines):
                results.append(tx)
        return sorted(results, key=lambda t: t.date)

    def _persist_period_lock(self, account_name: str, new_through: Optional[str], action: str) -> None:
        """Write a lock change and its audit-log row in one DB transaction.
        new_through=None removes the lock. Raises on failure (the mixin only
        updates memory after this returns)."""
        previous = self.period_locks.locked_through(account_name)
        now = datetime.now().isoformat(timespec="seconds")
        conn, cur = self._connect()
        try:
            if new_through is None:
                cur.execute("DELETE FROM sql_period_locks WHERE account_name = ?", (account_name,))
            else:
                cur.execute(
                    "INSERT INTO sql_period_locks (account_name, locked_through, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(account_name) DO UPDATE SET locked_through = excluded.locked_through, "
                    "updated_at = excluded.updated_at",
                    (account_name, new_through, now),
                )
            cur.execute(
                "INSERT INTO sql_period_lock_log (account_name, action, previous_through, new_through, logged_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (account_name, action, previous, new_through, now),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------------- Reconciliation ----------------

    RECONCILIATION_TOLERANCE = CENT

    def record_statement_balance(self, account_name: str, date: str, balance: Any, source: Optional[str] = None) -> Tuple[bool, str]:
        """
        Record a bank/card-reported running balance as of a specific
        date for an account -- a "checkpoint" the ledger's own computed
        balance can later be reconciled against. Typically called once
        per imported statement line that includes a balance column.
        """
        if account_name not in self.accounts:
            return False, f"Account '{account_name}' does not exist."
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except (ValueError, TypeError):
            return False, "Invalid date format. Use YYYY-MM-DD."
        if balance is None:
            return False, "No balance provided."

        m_balance = money(balance)
        conn, cur = self._connect()
        try:
            cur.execute(
                "INSERT INTO sql_statement_balances (account_name, date, balance, source, recorded_at) VALUES (?, ?, ?, ?, ?)",
                (account_name, date, str(m_balance), source, datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()
        return True, "Statement balance recorded."

    def get_statement_balances(self, account_name: Optional[str] = None) -> List[Dict[str, Any]]:
        conn, cur = self._connect()
        try:
            if account_name:
                cur.execute(
                    "SELECT id, account_name, date, balance, source, recorded_at FROM sql_statement_balances "
                    "WHERE account_name = ? ORDER BY date, id",
                    (account_name,),
                )
            else:
                cur.execute(
                    "SELECT id, account_name, date, balance, source, recorded_at FROM sql_statement_balances "
                    "ORDER BY account_name, date, id"
                )
            rows = cur.fetchall()
        finally:
            conn.close()
        return [
            {"id": r[0], "account_name": r[1], "date": r[2], "balance": money(r[3]), "source": r[4], "recorded_at": r[5]}
            for r in rows
        ]

    def delete_statement_balance(self, checkpoint_id: int) -> Tuple[bool, str]:
        conn, cur = self._connect()
        try:
            cur.execute("DELETE FROM sql_statement_balances WHERE id = ?", (checkpoint_id,))
            if cur.rowcount == 0:
                return False, "Checkpoint not found."
            conn.commit()
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()
        return True, "Checkpoint deleted."

    def running_balance_as_of(self, account_name: str, as_of_date: str) -> Decimal:
        """
        The ledger's own computed balance for an account, using only
        entries dated on or before as_of_date. Assumes dates are stored
        as normalized YYYY-MM-DD strings (enforced by record_transaction),
        so lexicographic comparison is chronological.
        """
        if account_name not in self.accounts:
            raise ValueError(f"Account '{account_name}' does not exist.")
        acc = self.accounts[account_name]
        total_dr = ZERO
        total_cr = ZERO
        for entry in acc.entries:
            if entry["date"] <= as_of_date:
                total_dr += entry["dr"]
                total_cr += entry["cr"]
        bal = total_dr - total_cr if acc.NORMAL_BALANCES[acc.account_type] == "DR" else total_cr - total_dr
        return money(bal)

    def reconciliation_report(self, account_name: Optional[str] = None, tolerance: Optional[Decimal] = None) -> List[Dict[str, Any]]:
        """
        Compare each recorded statement-balance checkpoint against the
        ledger's own running balance for that account as of the same
        date. One row per checkpoint; "matched" is True when the two
        balances agree within `tolerance` (default one cent).
        """
        if tolerance is None:
            tolerance = self.RECONCILIATION_TOLERANCE

        results = []
        for cp in self.get_statement_balances(account_name):
            try:
                ledger_balance = self.running_balance_as_of(cp["account_name"], cp["date"])
            except ValueError:
                continue
            difference = money(ledger_balance - cp["balance"])
            results.append(
                {
                    "account_name": cp["account_name"],
                    "date": cp["date"],
                    "statement_balance": cp["balance"],
                    "ledger_balance": ledger_balance,
                    "difference": difference,
                    "matched": abs(difference) <= tolerance,
                    "source": cp["source"],
                }
            )
        return results
