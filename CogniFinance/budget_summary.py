"""
budget_summary.py ver 0.1
For CogniFinance ver 1.9

One shared, read-only summary of the envelope budget for a month.

WHY THIS EXISTS
===============
Simple Mode's Home screen (envelope cards) and Plans screen (the full
envelope list) must never disagree about a number. Both therefore read
this one summary instead of computing their own.

THE CONTRACT
============
* Every number here is the budget engine's number, restated in a
  record. Nothing is recomputed, clamped, rounded differently or
  re-derived. In particular ``available`` may be negative.
* ``carried_in`` is the only derived value, and it is a plain
  subtraction of engine numbers (see EnvelopeSummary), so a card can
  say "includes X from earlier months" without hiding arithmetic.
* Read-only. This module never writes to the ledger. Writes go through
  the same ledger methods Pro Mode uses (assign_to_envelope,
  unassign_from_envelope, move_budget_money, upsert_budget_envelope).
* No Qt, no formatting, no currency symbols. Presentation belongs to
  the screen; the numbers belong here.

ORDER RULE (shown to the user wherever a list is truncated)
===========================================================
Overspent envelopes first, most overspent first; then the rest from
lowest Available to highest; ties by name. A screen that shows only
some envelopes must say "N of M" and link to the full list.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional

import budget_engine
from ledger import ZERO, money

STATE_OVERSPENT = "overspent"
STATE_OK = "ok"
STATE_EMPTY = "empty"


@dataclass(frozen=True)
class EnvelopeSummary:
    category: str
    linked_account: Optional[str]
    policy: str                 # "carry_forward" | "reset_monthly"
    assigned: Decimal           # engine: net assignment in the month
    spent: Decimal              # engine: expense debits in the month
    available: Decimal          # engine: envelope_available (may be < 0)
    carried_in: Decimal         # available - (assigned - spent)
    state: str                  # STATE_*
    tracks_spending: bool       # False => spent is always zero (no usable link)


@dataclass(frozen=True)
class BudgetSummary:
    month: str                  # "YYYY-MM"
    left_to_assign: Decimal     # engine: ready_to_assign (may be < 0)
    envelopes: List[EnvelopeSummary]   # already in ORDER RULE order

    @property
    def overspent(self) -> List[EnvelopeSummary]:
        return [e for e in self.envelopes if e.state == STATE_OVERSPENT]


_POLICY_SENTENCES = {
    "carry_forward": "Unspent money carries over to next month.",
    "reset_monthly": "Resets each month.",
}


def policy_sentence(policy: str) -> str:
    """One plain sentence for an envelope's rollover policy.

    Shared so Home and Plans word it identically. An unknown policy is
    reported as such rather than guessed at.
    """
    return _POLICY_SENTENCES.get(policy, "Rollover rule not recognized.")


def _state(assigned: Decimal, spent: Decimal, available: Decimal) -> str:
    if available < ZERO:
        return STATE_OVERSPENT
    if assigned == ZERO and spent == ZERO and available == ZERO:
        return STATE_EMPTY
    return STATE_OK


def summarize(ledger, month: str) -> BudgetSummary:
    """Summarize all ACTIVE envelopes for ``month`` (YYYY-MM)."""
    items: List[EnvelopeSummary] = []
    for env in ledger.get_budget_envelopes():          # active only
        category = env["category_name"]
        linked = env["linked_coa_account"]
        policy = env["rollover_policy"]

        assigned = budget_engine.net_assignment_in_month(ledger, category, month)
        tracks = bool(linked) and linked in ledger.accounts
        spent = ledger.get_category_actuals(linked, month) if tracks else ZERO
        available = budget_engine.envelope_available(ledger, category, month)
        carried_in = money(available - (assigned - spent))

        items.append(EnvelopeSummary(
            category=category,
            linked_account=linked,
            policy=policy,
            assigned=assigned,
            spent=spent,
            available=available,
            carried_in=carried_in,
            state=_state(assigned, spent, available),
            tracks_spending=tracks,
        ))

    items.sort(key=lambda e: (e.state != STATE_OVERSPENT,
                              e.available,
                              e.category.lower()))
    return BudgetSummary(
        month=month,
        left_to_assign=budget_engine.ready_to_assign(ledger, month),
        envelopes=items,
    )
