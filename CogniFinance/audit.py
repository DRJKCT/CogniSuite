"""
Audit log queries for the CogniFinance ledger.

Separated from ledger.py to keep that module focused on transaction
accounting. This module is read-only and only touches the
sql_transaction_history table. It opens its own SQLCipher connections
using the same passphrase as the main ledger, so it doesn't need to
share state with the GeneralLedger instance.
"""
import json
from typing import List, Dict, Any, Optional

from sqlcipher3 import dbapi2 as sqlite3

# Reuse the same SQLCipher settings as ledger.py so we're guaranteed
# to open the same database file with compatible parameters.
from ledger import SQLCIPHER_COMPATIBILITY, SQLCIPHER_KDF_ITER


class AuditLogReader:
    """Read-only access to the ledger's transaction history table."""

    def __init__(self, db_path: str, passphrase: str):
        self.db_path = db_path
        self.passphrase = passphrase

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        escaped = self.passphrase.replace("'", "''")
        cur.execute(f"PRAGMA key = '{escaped}'")
        cur.execute(f"PRAGMA cipher_compatibility = {SQLCIPHER_COMPATIBILITY}")
        cur.execute(f"PRAGMA kdf_iter = {SQLCIPHER_KDF_ITER}")
        # --- NEW: Wait up to 5 seconds if ledger.py is writing ---
        cur.execute("PRAGMA busy_timeout = 5000;")
        return conn, cur

    @staticmethod
    def _row_to_dict(row) -> Dict[str, Any]:
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

    @staticmethod
    def _build_where(start_date, end_date, change_type, search):
        clauses, params = [], []
        if start_date:
            clauses.append("date >= ?"); params.append(start_date)
        if end_date:
            clauses.append("date <= ?"); params.append(end_date)
        if change_type in ("created", "updated", "deleted"):
            clauses.append("change_type = ?"); params.append(change_type)
        if search:
            # Match against description OR any account name embedded in lines_json.
            clauses.append("(description LIKE ? OR lines_json LIKE ?)")
            like = f"%{search}%"
            params.extend([like, like])
        return clauses, params

    def get_audit_log(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        change_type: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Paginated, filterable audit log. Newest-first."""
        conn, cur = self._connect()
        try:
            clauses, params = self._build_where(start_date, end_date, change_type, search)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            params.extend([limit, offset])
            cur.execute(
                f"SELECT id, tx_id, change_type, changed_at, date, description, lines_json "
                f"FROM sql_transaction_history {where} "
                f"ORDER BY id DESC LIMIT ? OFFSET ?",
                params,
            )
            return [self._row_to_dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def count_audit_log(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        change_type: Optional[str] = None,
        search: Optional[str] = None,
    ) -> int:
        """Total matching rows — used to render pagination controls."""
        conn, cur = self._connect()
        try:
            clauses, params = self._build_where(start_date, end_date, change_type, search)
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            cur.execute(f"SELECT COUNT(*) FROM sql_transaction_history {where}", params)
            return cur.fetchone()[0]
        finally:
            conn.close()

    def get_history_for_tx(self, tx_id: int) -> List[Dict[str, Any]]:
        """Full history for a single transaction, oldest first."""
        conn, cur = self._connect()
        try:
            cur.execute(
                "SELECT id, tx_id, change_type, changed_at, date, description, lines_json "
                "FROM sql_transaction_history WHERE tx_id = ? ORDER BY id",
                (tx_id,),
            )
            return [self._row_to_dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
