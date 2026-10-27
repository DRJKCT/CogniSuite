# network_call_log.py
# For CogniFinance -- trust artifact for the investment/crypto price
# refresh feature.
#
# Deliberately dumb: this module knows nothing about WHY a network call
# was made, what triggered it, or what it means financially. It only
# records what price_fetch.py tells it -- timestamp, which API/endpoint,
# which symbols, and the outcome. No account data, no balances, no user
# identifiers ever pass through here, because price_fetch.py never
# has any of that to hand it in the first place (it only ever deals in
# ticker symbols).
#
# Same non-destructive ethos as the main ledger: append-only, never
# edited. The retention cap below exists purely for file-size hygiene,
# not to hide anything -- it drops the OLDEST rows, never a
# specific one, and only once the log has grown past the cap.

import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_CAP = 2000  # rows; hygiene only, not secrecy


def _init_db(gl):
    conn, cur = gl._connect()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sql_network_call_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                source TEXT NOT NULL,
                symbols TEXT NOT NULL,
                outcome TEXT NOT NULL,
                detail TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()


def log_call(gl, source: str, symbols: List[str], outcome: str, detail: str = "",
             retention_cap: int = DEFAULT_RETENTION_CAP) -> None:
    """Record one outbound network call. `outcome` should be
    'success' or 'failure'. Never raises on logging failure -- a
    logging problem should not be allowed to look like a fetch
    problem, or vice versa; it's reported via the app's normal logger
    instead."""
    _init_db(gl)
    conn, cur = gl._connect()
    try:
        cur.execute(
            "INSERT INTO sql_network_call_log (timestamp, source, symbols, outcome, detail) VALUES (?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), source, ", ".join(symbols), outcome, detail),
        )
        # Retention: drop oldest rows past the cap. Never touches the
        # rows it keeps.
        cur.execute("SELECT COUNT(*) FROM sql_network_call_log")
        count = cur.fetchone()[0]
        if count > retention_cap:
            cur.execute(
                "DELETE FROM sql_network_call_log WHERE id IN ("
                "  SELECT id FROM sql_network_call_log ORDER BY id ASC LIMIT ?"
                ")",
                (count - retention_cap,),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"network_call_log: failed to record call to '{source}': {e}")
    finally:
        conn.close()


def get_recent(gl, limit: int = 100) -> List[Dict]:
    """Newest first. This is what the UI's call-log panel and the Logs
    tab's filtered view both read from."""
    _init_db(gl)
    conn, cur = gl._connect()
    try:
        cur.execute(
            "SELECT timestamp, source, symbols, outcome, detail FROM sql_network_call_log "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [
            {"timestamp": r[0], "source": r[1], "symbols": r[2], "outcome": r[3], "detail": r[4]}
            for r in cur.fetchall()
        ]
    finally:
        conn.close()
