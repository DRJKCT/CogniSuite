# period_lock.py
"""
period_lock.py ver 1.1
For CogniFinance ver 1.9

Per-account period locking. Finishing a reconciliation locks the account
"through" the statement end date; any transaction that touches a locked
account on or before that date becomes read-only (no edit, void, un-reconcile,
re-account, or new posting into that window).

Pure Python, no Qt, no SQL. All dates are ISO "YYYY-MM-DD" strings, matching
Transaction.date, so plain string comparison orders them correctly.

Persistence is the ledger's job: the host class must provide
    self.accounts                 (dict of account name -> Account)
    self.transactions             (list of Transaction with .date and .lines)
    self._persist_period_lock(account, new_through_or_None, action)
        -> writes the change (and its audit-log row); raises on failure.
GeneralLedger in ledger.py implements that against sql_period_locks.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable, Optional, Tuple


class PeriodLockedError(ValueError):
    """Raised when something tries to change a transaction in a locked period.
    Subclasses ValueError so existing `except ValueError` handlers still catch it."""


class PeriodLocks:
    """In-memory map: account name -> ISO date the account is locked through (inclusive)."""

    def __init__(self, locks: Optional[dict] = None):
        self._locks: dict = dict(locks or {})

    def locked_through(self, account: str) -> Optional[str]:
        return self._locks.get(account)

    def is_locked(self, account: str, tx_date: str) -> bool:
        through = self._locks.get(account)
        return through is not None and tx_date <= through

    def set(self, account: str, through_date: Optional[str]) -> None:
        if through_date is None:
            self._locks.pop(account, None)
        else:
            self._locks[account] = through_date

    def rename(self, old: str, new: str) -> None:
        if old in self._locks:
            self._locks[new] = self._locks.pop(old)

    @staticmethod
    def previous_month_end(iso_date: str) -> str:
        """Last day of the month before the month containing iso_date."""
        y, m, _ = (int(p) for p in iso_date.split("-"))
        return (date(y, m, 1) - timedelta(days=1)).isoformat()


class PeriodLockMixin:
    """Mix into GeneralLedger (see module docstring for what the host provides)."""

    # ---- checks --------------------------------------------------------
    def period_lock_violation(self, tx_date: str, account_names: Iterable[str]) -> Optional[str]:
        """Return a user-facing message if a transaction dated tx_date touching
        any of account_names falls inside a locked period, else None."""
        hit = sorted({a for a in account_names if self.period_locks.is_locked(a, tx_date)})
        if not hit:
            return None
        through = self.period_locks.locked_through(hit[0])
        return (
            f"Period is locked for {', '.join(hit)} through {through}; "
            f"a transaction dated {tx_date} cannot be added or changed."
        )

    def is_tx_locked(self, tx) -> bool:
        return self.period_lock_violation(tx.date, [l.account_name for l in tx.lines]) is not None

    def assert_tx_unlocked(self, tx) -> None:
        msg = self.period_lock_violation(tx.date, [l.account_name for l in tx.lines])
        if msg:
            raise PeriodLockedError(msg)

    # ---- lock API used by ReconcileTab ---------------------------------
    def locked_through(self, account: str) -> Optional[str]:
        return self.period_locks.locked_through(account)

    def lock_period(self, account: str, through_date: str) -> Tuple[bool, str]:
        """Lock account through through_date. Never moves a lock backwards."""
        if account not in self.accounts:
            return False, "Account does not exist."
        try:
            datetime.strptime(through_date, "%Y-%m-%d")
        except ValueError:
            return False, "Invalid date format. Use YYYY-MM-DD."
        current = self.period_locks.locked_through(account)
        if current is not None and through_date <= current:
            return True, f"Already locked through {current}."
        try:
            self._persist_period_lock(account, through_date, "lock")
        except Exception as e:
            return False, f"Could not save period lock: {e}"
        self.period_locks.set(account, through_date)   # memory only after the DB write succeeded
        return True, f"{account} locked through {through_date}."

    def reopen_last_period(self, account: str) -> Tuple[bool, str]:
        """Roll the lock back to the end of the previous month. If that is
        earlier than the account's first transaction, the lock is removed."""
        current = self.period_locks.locked_through(account)
        if current is None:
            return True, "Period was not locked."
        new = PeriodLocks.previous_month_end(current)
        dates = [
            tx.date for tx in self.transactions
            if any(l.account_name == account for l in tx.lines)
        ]
        if not dates or new < min(dates):
            new = None
        try:
            self._persist_period_lock(account, new, "reopen")
        except Exception as e:
            return False, f"Could not save period change: {e}"
        self.period_locks.set(account, new)
        return True, (f"{account} reopened; now locked through {new}." if new
                      else f"{account} fully reopened.")
