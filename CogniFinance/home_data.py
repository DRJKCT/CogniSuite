"""
home_data.py ver 0.1
For CogniFinance ver 1.9

Everything the Simple Mode Home screen shows, as plain data.

The Home screen reads; it never computes its own numbers. Each figure
here comes from an existing ledger or budget-engine call, and the
tests check that the headline numbers match those calls exactly.

Scope rule (from the Simple Mode contract): any list that shows only
some of its items says how many exist. ``accounts_total`` and
``budget.total_envelopes`` carry that count so the screen can say
"4 of 9" instead of silently cutting the list.

No Qt, no formatting of money, no currency symbols.
"""
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Callable, List, Optional, Tuple

import budget_summary
from ledger import ZERO, money

# Accounts a person thinks of as "my accounts": cash-like assets and
# credit cards. An account with no report line set is included, because
# most personal ledgers never set one.
EVERYDAY_REPORT_LINES = (None, "Cash and Cash Equivalents", "Credit Cards")
EVERYDAY_TYPES = ("Asset", "Liability")


@dataclass(frozen=True)
class AccountRow:
    name: str
    balance: Decimal        # the ledger's balance, always as stored
    owed: bool              # True for liabilities: the balance is money owed


@dataclass(frozen=True)
class ActivityRow:
    date: str
    description: str
    category: str
    amount: Decimal         # the app's display amount (signed)


@dataclass(frozen=True)
class SpendingCard:
    month: str                              # "YYYY-MM"
    total: Decimal                          # ledger: expense_total for the month
    cumulative: List[Decimal]               # running total by day, ends at total
    top_categories: List[Tuple[str, Decimal]]


@dataclass(frozen=True)
class EnvelopeCardRow:
    category: str
    available: Decimal
    spent: Decimal
    funds: Decimal          # assigned + carried_in: what could be spent this month
    fraction: float         # spent / funds, clamped to 0..1
    state: str              # budget_summary.STATE_*
    policy: str


@dataclass(frozen=True)
class BudgetCard:
    month: str
    left_to_assign: Decimal
    shown: List[EnvelopeCardRow]
    total_envelopes: int
    overspent_count: int


@dataclass(frozen=True)
class HomeData:
    greeting: str
    date_line: str
    spending: SpendingCard
    accounts: List[AccountRow]
    accounts_total: int
    recent: List[ActivityRow]
    budget: BudgetCard


# ---------------------------------------------------------------------------

def greeting_for(now: datetime, display_name: Optional[str]) -> str:
    if now.hour < 12:
        phrase = "Good morning"
    elif now.hour < 17:
        phrase = "Good afternoon"
    else:
        phrase = "Good evening"
    name = (display_name or "").strip()
    return f"{phrase}, {name}!" if name else f"{phrase}!"


def date_line_for(today: date) -> str:
    return f"{today:%A, %B} {today.day}, {today.year}"


def bar_fraction(env: "budget_summary.EnvelopeSummary") -> float:
    """How much of this month's usable money has been spent, 0..1."""
    funds = env.assigned + env.carried_in
    if funds <= ZERO:
        return 1.0 if env.spent > ZERO else 0.0
    return float(min(env.spent / funds, Decimal(1)))


def _spending_card(ledger, today: date, top_n: int) -> SpendingCard:
    month = f"{today.year:04d}-{today.month:02d}"
    report = ledger.monthly_income_expense_report(today.year, today.month)
    total = report["expense_total"]

    last_day = monthrange(today.year, today.month)[1]
    start, end = f"{month}-01", f"{month}-{last_day:02d}"
    per_day = {}
    latest = today.day
    for tx in ledger.get_transactions(start_date=start, end_date=end):
        day = int(tx.date[8:10])
        for line in tx.lines:
            acc = ledger.accounts[line.account_name]
            if acc.account_type == "Expense" and line.dr != line.cr:
                # Net, like the ledger's own report: a refund or a void's
                # reversal lowers the running total instead of being ignored.
                per_day[day] = per_day.get(day, ZERO) + line.dr - line.cr
                latest = max(latest, day)
    running = ZERO
    cumulative = []
    for day in range(1, latest + 1):      # runs past today if a charge is future-dated
        running += per_day.get(day, ZERO)
        cumulative.append(money(running))

    ranked = sorted(((name, amount) for name, amount in report["expense_by_account"].items()
                     if amount > ZERO),
                    key=lambda kv: (-kv[1], kv[0].lower()))
    return SpendingCard(month=month, total=total, cumulative=cumulative,
                        top_categories=ranked[:top_n])


def _accounts(ledger, limit: int) -> Tuple[List[AccountRow], int]:
    rows = []
    for name, acc in ledger.accounts.items():
        if acc.account_type not in EVERYDAY_TYPES:
            continue
        if getattr(acc, "report_line", None) not in EVERYDAY_REPORT_LINES:
            continue
        rows.append(AccountRow(name=name, balance=acc.get_balance(),
                               owed=(acc.account_type == "Liability")))
    # Money you have first (largest first), then money you owe (largest first).
    rows.sort(key=lambda r: (r.owed, -r.balance, r.name.lower()))
    return rows[:limit], len(rows)


def everyday_accounts(ledger, limit: int) -> Tuple[List[AccountRow], int]:
    """Public name for the accounts list used by Home and its quick-add."""
    return _accounts(ledger, limit)


def category_label(ledger, tx) -> str:
    """What a transaction was for: its category, \"Split (n)\", or \"Transfer\"."""
    return _category_label(ledger, tx)


def _category_label(ledger, tx) -> str:
    names = []
    for line in tx.lines:
        acc = ledger.accounts.get(line.account_name)
        if acc is not None and acc.account_type in ("Expense", "Revenue"):
            if line.account_name not in names:
                names.append(line.account_name)
    if len(names) == 1:
        return names[0]
    if len(names) > 1:
        return f"Split ({len(names)})"
    return "Transfer"


def _recent(ledger, amount_fn: Callable, limit: int) -> List[ActivityRow]:
    """Latest transactions, newest first.

    A voided transaction and the reversal that cancels it are left out
    together (they net to nothing). The full list is on the Money screen.
    """
    out = []
    for tx in reversed(ledger.transactions):
        if getattr(tx, "is_voided", False) or getattr(tx, "reverses_tx_id", None):
            continue
        out.append(ActivityRow(date=tx.date, description=tx.description,
                               category=_category_label(ledger, tx),
                               amount=amount_fn(ledger, tx)))
        if len(out) == limit:
            break
    return out


def _budget_card(ledger, month: str, limit: int) -> BudgetCard:
    summary = budget_summary.summarize(ledger, month)
    rows = []
    for env in summary.envelopes[:limit]:
        rows.append(EnvelopeCardRow(
            category=env.category,
            available=env.available,
            spent=env.spent,
            funds=env.assigned + env.carried_in,
            fraction=bar_fraction(env),
            state=env.state,
            policy=env.policy,
        ))
    return BudgetCard(month=month, left_to_assign=summary.left_to_assign,
                      shown=rows, total_envelopes=len(summary.envelopes),
                      overspent_count=len(summary.overspent))


def build_home_data(ledger, now: datetime, amount_fn: Callable,
                    display_name: Optional[str] = None,
                    recent_limit: int = 8, account_limit: int = 4,
                    envelope_limit: int = 4, top_categories: int = 3) -> HomeData:
    today = now.date()
    spending = _spending_card(ledger, today, top_categories)
    accounts, accounts_total = _accounts(ledger, account_limit)
    return HomeData(
        greeting=greeting_for(now, display_name),
        date_line=date_line_for(today),
        spending=spending,
        accounts=accounts,
        accounts_total=accounts_total,
        recent=_recent(ledger, amount_fn, recent_limit),
        budget=_budget_card(ledger, spending.month, envelope_limit),
    )
