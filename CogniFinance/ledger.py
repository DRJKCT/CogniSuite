#Ledger v1.2
#For CogniFinance ver 1.95

import os
import glob
import hashlib
import json
import logging
import re
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import List, Dict, Any, Tuple, Optional
from dataclasses import dataclass, field
from datetime import datetime
from sqlcipher3 import dbapi2 as sqlite3
from calendar import monthrange

from config import BACKUP_DIR

# Currency Constants and Helper
ZERO = Decimal("0.00")
CENT = Decimal("0.01")

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

def _sanitize_for_excel(value):
    """Strips illegal XML control characters from strings to prevent openpyxl crashes."""
    if isinstance(value, str):
        return _ILLEGAL_XML_RE.sub('', value)
        if value and value[0] in _FORMULA_INJECTION_CHARS:
            value = "'" + value
    return value


class Account:
    NORMAL_BALANCES = {
        "Asset": "DR",
        "Expense": "DR",
        "Liability": "CR",
        "Equity": "CR",
        "Revenue": "CR",
    }

    def __init__(self, name: str, account_type: str):
        if account_type not in self.NORMAL_BALANCES:
            raise ValueError("Invalid account type.")
        self.name = name
        self.account_type = account_type
        self.entries: List[Dict[str, Any]] = []

    def add_entry(self, date: str, description: str, dr: Decimal, cr: Decimal):
        self.entries.append({"date": date, "description": description, "dr": money(dr), "cr": money(cr)})

    def get_balance(self) -> Decimal:
        total_dr = sum((e["dr"] for e in self.entries), ZERO)
        total_cr = sum((e["cr"] for e in self.entries), ZERO)
        bal = total_dr - total_cr if self.NORMAL_BALANCES[self.account_type] == "DR" else total_cr - total_dr
        return money(bal)


class GeneralLedger:
    def __init__(self, db_path: str, dek_hex: str):
        if not dek_hex:
            raise ValueError("A DEK hex string is strictly required to unlock the ledger data")

        self.db_path = db_path
        self.dek_hex = dek_hex
        self.accounts: Dict[str, Account] = {}
        self.transactions: List[Transaction] = []
        self.budgets: Dict[Tuple[str, str], Budget] = {}
        self.category_rules: List[CategoryRule] = []

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

            cur.execute("CREATE TABLE IF NOT EXISTS sql_accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, account_type TEXT)")

            cur.execute("CREATE TABLE IF NOT EXISTS sql_transactions ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "date TEXT, description TEXT, imported_id TEXT, "
                    "reconciled INTEGER DEFAULT 0, "
                    "is_voided INTEGER DEFAULT 0, "
                    "reversed_by_tx_id INTEGER, "
                    "reverses_tx_id INTEGER)")

            cur.execute("CREATE TABLE IF NOT EXISTS sql_transaction_lines (id INTEGER PRIMARY KEY AUTOINCREMENT, tx_id INTEGER, account_name TEXT, dr TEXT, cr TEXT, FOREIGN KEY(tx_id) REFERENCES sql_transactions(id))")
            cur.execute("CREATE TABLE IF NOT EXISTS sql_category_rules (id INTEGER PRIMARY KEY AUTOINCREMENT, keyword TEXT, category TEXT, case_sensitive INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1)")
            cur.execute("CREATE TABLE IF NOT EXISTS sql_budgets (id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT, month TEXT, amount TEXT, UNIQUE(category, month))")
            cur.execute(
                "CREATE TABLE IF NOT EXISTS sql_statement_balances ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "account_name TEXT, "
                "date TEXT, "
                "balance TEXT, "
                "source TEXT, "
                "recorded_at TEXT)"
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
                "prev_hash TEXT, "   # NEW: Hash of the previous row
                "row_hash TEXT)"     # NEW: Hash of this row
            )
            cur.execute("PRAGMA table_info(sql_transactions)")
            columns = [info[1] for info in cur.fetchall()]

            if "is_voided" not in columns:
                cur.execute("ALTER TABLE sql_transactions ADD COLUMN is_voided INTEGER DEFAULT 0")
            if "reversed_by_tx_id" not in columns:
                cur.execute("ALTER TABLE sql_transactions ADD COLUMN reversed_by_tx_id INTEGER")
            if "reverses_tx_id" not in columns:
                cur.execute("ALTER TABLE sql_transactions ADD COLUMN reverses_tx_id INTEGER")
            conn.commit()
        finally:
            conn.close()
        self._harden_file_permissions(self.db_path)

    def _load_from_db(self):
        self.accounts = {}
        self.transactions = []
        self.budgets = {}
        self.category_rules = []

        conn, cur = self._connect()
        try:
            cur.execute("SELECT name, account_type FROM sql_accounts")
            rows = cur.fetchall()

            if not rows:
                seeds = [
                    ("Checking Account", "Asset"),
                    ("Salary Income", "Revenue"),
                    ("Groceries", "Expense"),
                    ("Uncategorized Expense", "Expense"),
                    ("Uncategorized Income", "Revenue"),
                    ("Credit Card", "Liability"),
                ]
                for name, acc_type in seeds:
                    cur.execute("INSERT INTO sql_accounts (name, account_type) VALUES (?, ?)", (name, acc_type))
                conn.commit()
                cur.execute("SELECT name, account_type FROM sql_accounts")
                rows = cur.fetchall()

            for name, acc_type in rows:
                self.accounts[name] = Account(name, acc_type)

            cur.execute("SELECT id, date, description, imported_id, reconciled FROM sql_transactions WHERE is_voided = 0 ORDER BY date")
            for tx_id, date, desc, imp_id, rec in cur.fetchall():
                cur.execute("SELECT id, account_name, dr, cr FROM sql_transaction_lines WHERE tx_id = ?", (tx_id,))
                lines = [TransactionLine(acc_name, money(dr) if dr else ZERO, money(cr) if cr else ZERO, id=line_id) for line_id, acc_name, dr, cr in cur.fetchall()]
                tx = Transaction(tx_id, date, desc, lines, imp_id, bool(rec))
                self._apply_transaction_to_accounts(tx)
                self.transactions.append(tx)

            cur.execute("SELECT category, month, amount FROM sql_budgets")
            for cat, month, amount in cur.fetchall():
                self.budgets[(cat, month)] = Budget(cat, month, money(amount))

            cur.execute("SELECT id, keyword, category, case_sensitive, enabled FROM sql_category_rules ORDER BY id")
            for rid, kw, cat, cs, en in cur.fetchall():
                self.category_rules.append(CategoryRule(rid, kw, cat, bool(cs), bool(en)))
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

    def create_account(self, name: str, acc_type: str) -> Tuple[bool, str]:
        if name in self.accounts:
            return False, "Account already exists."
        conn, cur = self._connect()
        try:
            cur.execute("INSERT INTO sql_accounts (name, account_type) VALUES (?, ?)", (name, acc_type))
            conn.commit()
            self.accounts[name] = Account(name, acc_type)
            return True, f"Account '{name}' created."
        except ValueError:
            return False, "Invalid Type."
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
            conn.commit()
            del self.accounts[name]
            return True, f"Account '{name}' deleted."
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()

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
                cur.execute("UPDATE sql_budgets SET category = ? WHERE category = ?", (new_name, old_name))
                cur.execute("UPDATE sql_category_rules SET category = ? WHERE category = ?", (new_name, old_name))

            if type_changed:
                target_name = new_name if name_changed else old_name
                cur.execute("UPDATE sql_accounts SET account_type = ? WHERE name = ?", (new_type, target_name))

            conn.commit()

            # Update in-memory state
            if name_changed:
                acc = self.accounts.pop(old_name)
                self.accounts[new_name] = acc
                acc.name = new_name

                # Update budgets dictionary keys
                new_budgets = {}
                for (cat, month), b in self.budgets.items():
                    if cat == old_name:
                        new_budgets[(new_name, month)] = Budget(new_name, month, b.amount)
                    else:
                        new_budgets[(cat, month)] = b
                self.budgets = new_budgets

                # Update rules in memory
                for rule in self.category_rules:
                    if rule.category == old_name:
                        rule.category = new_name

            if type_changed:
                acc.account_type = new_type

            return True, "Account updated."
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

    def _log_history(self, cur, tx_id: Optional[int], change_type: str, date: str, description: str, lines) -> None:
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
        payload = f"{tx_id}{change_type}{changed_at}{date}{description}{lines_json}{prev_hash}"
        row_hash = hashlib.sha256(payload.encode('utf-8')).hexdigest()

        # 4. Insert with the hashes
        cur.execute(
            "INSERT INTO sql_transaction_history (tx_id, change_type, changed_at, date, description, lines_json, prev_hash, row_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (tx_id, change_type, changed_at, date, description, lines_json, prev_hash, row_hash),
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

    def set_budget(self, category: str, month: str, amount: Any) -> Tuple[bool, str]:
        try:
            datetime.strptime(month, "%Y-%m")
        except ValueError:
            return False, "Invalid month format (use YYYY-MM)."

        m_amount = money(amount)
        conn, cur = self._connect()
        try:
            cur.execute(
                "INSERT INTO sql_budgets (category, month, amount) VALUES (?, ?, ?) "
                "ON CONFLICT(category, month) DO UPDATE SET amount = excluded.amount",
                (category, month, str(m_amount)),
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()
        self.budgets[(category, month)] = Budget(category, month, m_amount)
        return True, "Budget saved."

    def get_budget(self, category: str, month: str) -> Optional[Budget]:
        return self.budgets.get((category, month))

    def delete_budget(self, category: str, month: str) -> Tuple[bool, str]:
        conn, cur = self._connect()
        try:
            cur.execute(
                "DELETE FROM sql_budgets WHERE category = ? AND month = ?",
                (category, month),
            )
            if cur.rowcount == 0:
                conn.rollback()
                return False, "Budget not found."
            conn.commit()
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()

        self.budgets.pop((category, month), None)
        return True, "Budget deleted."

    def get_all_budgets(self) -> List[Budget]:
        return list(self.budgets.values())

    def spending_by_category(self, month: str) -> Dict[str, Decimal]:
        spending: Dict[str, Decimal] = {}
        for tx in self.transactions:
            if not tx.date.startswith(month):
                continue
            for line in tx.lines:
                acc = self.accounts[line.account_name]
                if acc.account_type == "Expense" and line.dr > ZERO:
                    spending[line.account_name] = spending.get(line.account_name, ZERO) + line.dr
        return {k: money(v) for k, v in spending.items()}

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

    def export_journal_csv(self, path: str):
        import csv
        rows = []
        for tx in self.transactions:
            for line in tx.lines:
                rows.append([tx.date, tx.description, line.account_name, self.accounts[line.account_name].account_type, line.dr if line.dr > ZERO else "", line.cr if line.cr > ZERO else ""])
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

    def expenses_by_category(
        self, start_date: str, end_date: str, category_map: Dict[str, List[str]]
    ) -> Dict[str, Decimal]:
        """Aggregate expenses strictly for accounts defined in category_map. Unmapped expenses are ignored."""
        if not category_map:
            return {}

        account_to_category = {}
        for cat, accounts in category_map.items():
            if isinstance(accounts, str):
                accounts = [accounts]
            for acc in accounts:
                account_to_category[acc] = cat

        totals = {cat: ZERO for cat in category_map.keys()}
        txs = self.get_business_expense_transactions(start_date, end_date, category_map)

        for tx in txs:
            for line in tx.lines:
                acc = self.accounts.get(line.account_name)
                if not acc or acc.account_type != "Expense" or line.dr <= ZERO:
                    continue

                # Only process accounts that belong to an eligible business category
                if line.account_name in account_to_category:
                    cat = account_to_category[line.account_name]
                    totals[cat] += line.dr

        return {k: money(v) for k, v in totals.items()}

    def medical_expenses_total(
        self, start_date: str, end_date: str, medical_accounts: List[str]
    ) -> Decimal:
        if not medical_accounts:
            return ZERO
        total = ZERO
        txs = self.get_transactions(start_date=start_date, end_date=end_date)
        for tx in txs:
            for line in tx.lines:
                acc = self.accounts.get(line.account_name)
                if acc and acc.account_type == "Expense" and line.dr > ZERO:
                    if line.account_name in medical_accounts:
                        total += line.dr
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

    def void_transaction(self, tx_id: int) -> Tuple[bool, str]:
        """
        APPEND-ONLY DELETE: Voids the original transaction by creating a
        perfect reversal (flipping DR/CR) and marking the original as voided.
        """
        conn, cur = self._connect()
        try:
            # 1. Fetch original state
            cur.execute("SELECT date, description, reconciled FROM sql_transactions WHERE id = ? AND is_voided = 0", (tx_id,))
            row = cur.fetchone()
            if not row:
                return False, "Transaction not found or already voided."
            old_date, old_desc, reconciled = row

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

    def edit_transaction(self, tx_id: int, new_date: str, new_description: str, new_lines: List[Tuple[Optional[int], str, Any, Any]]) -> Tuple[bool, str]:
        """
        APPEND-ONLY EDIT: Voids the original transaction and creates a brand new
        one with the updated details. Preserves the immutable audit trail.
        """
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

        conn, cur = self._connect()
        try:
            # 2. Fetch original state
            cur.execute("SELECT date, description, reconciled, imported_id  FROM sql_transactions WHERE id = ? AND is_voided = 0", (tx_id,))
            row = cur.fetchone()
            if not row:
                return False, "Transaction not found or already voided."
            old_date, old_desc, reconciled, old_imp_id = row

            if reconciled:
                return False, "Cannot edit a reconciled transaction. Unreconcile it first."

            cur.execute("SELECT id, account_name, dr, cr FROM sql_transaction_lines WHERE tx_id = ?", (tx_id,))
            old_lines = [(lid, acc, money(dr) if dr else ZERO, money(cr) if cr else ZERO) for lid, acc, dr, cr in cur.fetchall()]

            # 3. Mark original as voided
            cur.execute("UPDATE sql_transactions SET is_voided = 1 WHERE id = ?", (tx_id,))

            # 4. Insert new transaction
            cur.execute(
                "INSERT INTO sql_transactions (date, description, imported_id, reconciled, is_voided, reverses_tx_id) "
                "VALUES (?, ?, ?, 0, 0, ?)",
                (new_date, new_description, old_imp_id, tx_id)
            )
            new_tx_id = cur.lastrowid

            for acc, dr, cr in parsed:
                cur.execute(
                    "INSERT INTO sql_transaction_lines (tx_id, account_name, dr, cr) VALUES (?, ?, ?, ?)",
                    (new_tx_id, acc, str(dr), str(cr))
                )

            # 5. Log to history
            self._log_history(cur, tx_id, "voided", old_date, old_desc, old_lines)
            self._log_history(cur, new_tx_id, "created", new_date, new_description, [(None, acc, dr, cr) for acc, dr, cr in parsed])

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
                "SELECT id, tx_id, change_type, changed_at, date, description, lines_json, prev_hash, row_hash "
                "FROM sql_transaction_history ORDER BY id ASC"
            )
            rows = cur.fetchall()

            expected_prev_hash = "0" * 64
            for row in rows:
                hid, tx_id, change_type, changed_at, date, description, lines_json, stored_prev_hash, stored_row_hash = row

                if stored_prev_hash != expected_prev_hash:
                    return False, f"Chain broken at history ID {hid}: expected prev_hash {expected_prev_hash[:16]}..., got {stored_prev_hash[:16]}..."

                # Recompute the hash
                payload = f"{tx_id}{change_type}{changed_at}{date}{description}{lines_json}{stored_prev_hash}"
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

    def monthly_income_expense_report(self, year: int, month: int) -> Dict[str, Any]:
        start = f"{year:04d}-{month:02d}-01"
        last_day = monthrange(year, month)[1]
        end = f"{year:04d}-{month:02d}-{last_day:02d}"
        income_total = ZERO
        expense_total = ZERO
        income_by_account: Dict[str, Decimal] = {}
        expense_by_account: Dict[str, Decimal] = {}
        txs = self.get_transactions(start_date=start, end_date=end)
        for tx in txs:
            for line in tx.lines:
                acc = self.accounts[line.account_name]
                if acc.account_type == "Revenue" and line.cr > ZERO:
                    income_total += line.cr
                    income_by_account[line.account_name] = income_by_account.get(line.account_name, ZERO) + line.cr
                elif acc.account_type == "Expense" and line.dr > ZERO:
                    expense_total += line.dr
                    expense_by_account[line.account_name] = expense_by_account.get(line.account_name, ZERO) + line.dr
        return {
            "income_total": money(income_total),
            "expense_total": money(expense_total),
            "net": money(income_total - expense_total),
            "income_by_account": {k: money(v) for k, v in income_by_account.items()},
            "expense_by_account": {k: money(v) for k, v in expense_by_account.items()}
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
                amount = line.cr if acc.account_type == "Revenue" else line.dr
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
