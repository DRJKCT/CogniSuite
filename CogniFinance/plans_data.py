"""
plans_data.py ver 0.1
For CogniFinance ver 0.9.67

The small decisions behind the Simple Mode Plans screen, kept out of the
widgets so they can be tested: which month is showing, which accounts an
envelope may still be linked to, who can cover an overspent envelope.

No Qt. No new numbers: every amount still comes from budget_summary,
which comes from the budget engine.
"""
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

import budget_summary
from ledger import ZERO


def month_of(now: datetime) -> str:
    return f"{now.year:04d}-{now.month:02d}"


def shift_month(month: str, delta: int) -> str:
    year, number = int(month[:4]), int(month[5:7])
    index = year * 12 + (number - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_title(month: str) -> str:
    return datetime(int(month[:4]), int(month[5:7]), 1).strftime("%B %Y")


def linked_accounts(ledger, exclude_category: Optional[str] = None) -> set:
    """Expense accounts already tracked by an envelope (active or not).

    Two envelopes on one account would each count the same spending, so
    an account may belong to only one.
    """
    return {env["linked_coa_account"]
            for env in ledger.get_budget_envelopes(include_inactive=True)
            if env["linked_coa_account"] and env["category_name"] != exclude_category}


def free_expense_accounts(ledger, keep: Optional[str] = None,
                          exclude_category: Optional[str] = None) -> List[str]:
    """Expense accounts an envelope could be linked to, plus ``keep``."""
    used = linked_accounts(ledger, exclude_category)
    names = [name for name, acc in ledger.accounts.items()
             if acc.account_type == "Expense" and (name not in used or name == keep)]
    return sorted(names, key=str.lower)


def validate_new_envelope(ledger, name: str, linked: Optional[str]) -> Optional[str]:
    """A message to show the user, or None if the envelope may be created."""
    name = (name or "").strip()
    if not name:
        return "Give the envelope a name."
    existing = {e["category_name"].lower(): e
                for e in ledger.get_budget_envelopes(include_inactive=True)}
    if name.lower() in existing:
        if not existing[name.lower()]["is_active"]:
            return f"\"{name}\" already exists but is archived. Restore it from the full Budgets screen."
        return f"An envelope named \"{name}\" already exists."
    if not linked:
        return "Choose the spending account this envelope tracks."
    if linked not in ledger.accounts or ledger.accounts[linked].account_type != "Expense":
        return "That account is not an expense account."
    if linked in linked_accounts(ledger):
        return f"{linked} is already tracked by another envelope."
    return None


def shortfall(env: "budget_summary.EnvelopeSummary") -> Decimal:
    """How much an overspent envelope is short by (zero if it is not)."""
    return -env.available if env.available < ZERO else ZERO


def cover_sources(summary: "budget_summary.BudgetSummary", target: str):
    """Envelopes that have money to give, most available first."""
    sources = [e for e in summary.envelopes if e.category != target and e.available > ZERO]
    return sorted(sources, key=lambda e: (-e.available, e.category.lower()))


def carried_sentence(env: "budget_summary.EnvelopeSummary", money_text) -> str:
    """One plain line about money brought over from earlier months."""
    if env.carried_in > ZERO:
        return f"Includes {money_text(env.carried_in)} carried over from earlier months."
    if env.carried_in < ZERO:
        return f"Starts {money_text(-env.carried_in)} short from earlier months."
    return ""
