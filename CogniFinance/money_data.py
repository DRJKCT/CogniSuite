"""
money_data.py ver 0.1
For CogniFinance ver 0.9.67

Everything the Simple Mode Money screen lists, as plain data.

Same engine as Pro: the rows come from GeneralLedger.get_transactions,
the amounts from the app's own display-amount function, and the lock
and reconciled flags from the ledger. Nothing is recomputed here.

Scope rules: the list says how many matched and how many are shown, and
says how many voided or reversing entries it left out. The full list,
with those entries, is the Pro Transactions screen.

No Qt, no money formatting.
"""
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Callable, List, Optional, Tuple

import home_data

PERIOD_CHOICES: List[Tuple[str, str]] = [
    ("This month", "this_month"),
    ("Last month", "last_month"),
    ("Last 90 days", "last_90"),
    ("This year", "this_year"),
    ("All time", "all"),
]

KIND_SIMPLE = "simple"        # one category line + one account line
KIND_TRANSFER = "transfer"    # two account lines
KIND_COMPLEX = "complex"      # anything else (splits, 3+ lines)


@dataclass(frozen=True)
class TxRowData:
    tx_id: Optional[int]
    date: str
    description: str
    category: str
    account: str
    amount: Decimal
    is_split: bool
    reconciled: bool
    locked: bool
    edit_kind: str


@dataclass(frozen=True)
class DayGroup:
    date: str
    label: str
    rows: List[TxRowData]


@dataclass(frozen=True)
class MoneyData:
    period_key: str
    period_label: str
    days: List[DayGroup]
    shown: int
    total: int                # matches before the show-more limit
    hidden_count: int         # voided originals and reversals left out


def period_range(key: str, today: date) -> Tuple[Optional[str], Optional[str]]:
    """Inclusive ISO start and end dates for a period (None = unbounded)."""
    if key == "this_month":
        start = today.replace(day=1)
        end = today.replace(day=monthrange(today.year, today.month)[1])
    elif key == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    elif key == "last_90":
        start, end = today - timedelta(days=89), today
    elif key == "this_year":
        start, end = date(today.year, 1, 1), date(today.year, 12, 31)
    else:
        return None, None
    return start.isoformat(), end.isoformat()


def _type_of(ledger, line) -> str:
    acc = ledger.accounts.get(line.account_name)
    return acc.account_type if acc is not None else ""


def edit_kind(ledger, tx) -> str:
    """How much of this transaction Simple Mode can edit directly."""
    if len(tx.lines) != 2:
        return KIND_COMPLEX
    kinds = [_type_of(ledger, line) for line in tx.lines]
    everyday = [k in home_data.EVERYDAY_TYPES for k in kinds]
    category = [k in ("Expense", "Revenue") for k in kinds]
    if sum(everyday) == 1 and sum(category) == 1:
        return KIND_SIMPLE
    if all(everyday):
        return KIND_TRANSFER
    return KIND_COMPLEX


def account_label(ledger, tx) -> str:
    lines = [l for l in tx.lines if _type_of(ledger, l) in home_data.EVERYDAY_TYPES]
    if len(lines) == 1:
        return lines[0].account_name
    if len(lines) == 2 and len(tx.lines) == 2:
        source = next((l for l in lines if l.cr > 0), lines[0])
        target = next((l for l in lines if l.dr > 0), lines[1])
        return f"{source.account_name} \u2192 {target.account_name}"
    return "Multiple accounts" if lines else ""


def _row(ledger, tx, amount_fn: Callable) -> TxRowData:
    categories = [l for l in tx.lines if _type_of(ledger, l) in ("Expense", "Revenue")]
    return TxRowData(
        tx_id=tx.id,
        date=tx.date,
        description=tx.description,
        category=home_data.category_label(ledger, tx),
        account=account_label(ledger, tx),
        amount=amount_fn(ledger, tx),
        is_split=len(categories) > 1,
        reconciled=bool(getattr(tx, "reconciled", False)),
        locked=bool(ledger.is_tx_locked(tx)),
        edit_kind=edit_kind(ledger, tx),
    )


def _day_label(iso: str) -> str:
    try:
        d = datetime.strptime(iso[:10], "%Y-%m-%d").date()
    except ValueError:
        return iso
    return f"{d:%A, %b} {d.day}"


def build_money_data(ledger, today: date, amount_fn: Callable,
                     period: str = "this_month", account: Optional[str] = None,
                     search: Optional[str] = None, limit: int = 50) -> MoneyData:
    start, end = period_range(period, today)
    txs = ledger.get_transactions(start_date=start, end_date=end,
                                  account=account or None, search=search or None)
    kept, hidden = [], 0
    for tx in txs:
        if getattr(tx, "is_voided", False) or getattr(tx, "reverses_tx_id", None):
            hidden += 1
        else:
            kept.append(tx)
    kept.sort(key=lambda t: (t.date, t.id or 0), reverse=True)

    days: List[DayGroup] = []
    for tx in kept[:limit]:
        row = _row(ledger, tx, amount_fn)
        if not days or days[-1].date != tx.date:
            days.append(DayGroup(date=tx.date, label=_day_label(tx.date), rows=[]))
        days[-1].rows.append(row)

    label = next((name for name, key in PERIOD_CHOICES if key == period), period)
    return MoneyData(period_key=period, period_label=label, days=days,
                     shown=min(limit, len(kept)), total=len(kept), hidden_count=hidden)
