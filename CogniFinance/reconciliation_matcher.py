"""
reconciliation_matcher.py

Fuzzy-matches a pending transaction (e.g. mobile-synced, unreconciled)
against a set of candidate transactions (e.g. freshly-parsed bank
statement lines) and classifies each candidate into a match tier.

Deliberately has ZERO dependency on where either side's data actually
lives -- it only needs a date, an amount, and a description from each
side. This is what lets ofx_import.py, pdf_import.py, and any future
import source all call the SAME matching logic instead of each
re-implementing it (see the "boundaries" discussion this was built to
satisfy -- a parser's job is turning a file into transaction data, not
deciding how that data relates to what's already in the ledger).

Tiers (per your v0.9 design):
  GREEN  -- date (tight window) AND amount (exact) AND merchant (fuzzy match)
  YELLOW -- amount (exact) AND EITHER date (wider window) OR merchant (fuzzy match)
  RED    -- doesn't clear YELLOW's bar

Amount is a hard, exact gate for anything above RED -- no tolerance.
Real transaction amounts should match exactly; a tolerance risks
matching two coincidentally-close but genuinely different transactions,
which is a worse outcome than a real match landing in YELLOW instead of
GREEN and needing one extra click to confirm.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal
from difflib import SequenceMatcher
from enum import Enum
from typing import List


class MatchTier(Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


# Tunable thresholds -- named constants, not magic numbers buried in the
# function body, so these can be adjusted later without spelunking.
GREEN_DATE_WINDOW_DAYS = 1
YELLOW_DATE_WINDOW_DAYS = 10
MERCHANT_SIMILARITY_THRESHOLD = 0.6


@dataclass(frozen=True)
class MatchCandidate:
    """
    One side of a potential match. Deliberately generic -- plain fields,
    not tied to any specific table -- so this module works identically
    whether `id` is a mobile uuid, a ledger transaction id, or a raw
    statement-line index that doesn't exist as a row anywhere yet.

    `amount` should be the absolute value -- direction/sign isn't this
    module's concern, since both sides of a real match represent the
    same real-world dollar amount regardless of which account (asset vs
    liability) it's expressed against.

    `description` should be the most merchant-like text available on
    each side -- for a mobile transaction, that's the PAYEE field (often
    OCR'd from the actual receipt), NOT the expense category. Comparing
    a bank's "WALMART SUPERCENTER #4471" against a category label like
    "Groceries" would score poorly regardless of how good the real
    match is; comparing it against a payee of "Walmart" works far
    better, since both sides are then actually naming the same store.
    """
    id: str
    date: date_type
    amount: Decimal
    description: str


@dataclass(frozen=True)
class ScoredMatch:
    candidate: MatchCandidate
    tier: MatchTier
    date_diff_days: int
    merchant_similarity: float


def _normalize(text: str) -> str:
    return text.strip().lower()


def _merchant_similarity(a: str, b: str) -> float:
    """
    Character-level similarity ratio (0.0-1.0) via difflib -- no
    external dependency, and good enough for this use case since we're
    not doing anything more sophisticated than "does this look like the
    same store".

    Also checks substring containment as an alternate path to a match:
    difflib's ratio() can score misleadingly low on very different-
    length strings even when one clearly contains the other (e.g.
    "Walmart" vs "WALMART SUPERCENTER #4471" -- a real, common pattern
    between a mobile payee and a bank's fuller merchant string). If the
    shorter string appears inside the longer one, that's treated as a
    strong match (score 1.0) regardless of the raw ratio.
    """
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return 0.0
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    if shorter in longer:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def classify_match(pending: MatchCandidate, candidate: MatchCandidate) -> ScoredMatch:
    """Classify a single (pending, candidate) pair into a match tier."""
    date_diff_days = abs((pending.date - candidate.date).days)
    merchant_similarity = _merchant_similarity(pending.description, candidate.description)
    merchant_matches = merchant_similarity >= MERCHANT_SIMILARITY_THRESHOLD

    if pending.amount != candidate.amount:
        tier = MatchTier.RED
    elif date_diff_days <= GREEN_DATE_WINDOW_DAYS and merchant_matches:
        tier = MatchTier.GREEN
    elif date_diff_days <= YELLOW_DATE_WINDOW_DAYS or merchant_matches:
        tier = MatchTier.YELLOW
    else:
        tier = MatchTier.RED

    return ScoredMatch(
        candidate=candidate,
        tier=tier,
        date_diff_days=date_diff_days,
        merchant_similarity=merchant_similarity,
    )


def find_candidate_matches(
    pending: MatchCandidate,
    candidates: List[MatchCandidate],
    include_red: bool = False,
) -> List[ScoredMatch]:
    """
    Score every candidate against `pending`, sorted best-first (GREEN
    before YELLOW before RED; within a tier, closer date then higher
    merchant similarity). RED matches are excluded by default -- pass
    include_red=True if a caller specifically wants the "no likely
    match" case represented rather than just an empty/short list.
    """
    scored = [classify_match(pending, c) for c in candidates]
    if not include_red:
        scored = [s for s in scored if s.tier != MatchTier.RED]

    tier_order = {MatchTier.GREEN: 0, MatchTier.YELLOW: 1, MatchTier.RED: 2}
    scored.sort(key=lambda s: (tier_order[s.tier], s.date_diff_days, -s.merchant_similarity))
    return scored
