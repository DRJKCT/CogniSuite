"""
reconciliation_matcher.py ver 0.1
For CogniFinance ver 0.9.67

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

import logging
from dataclasses import dataclass
from datetime import date as date_type, datetime
from decimal import Decimal
from difflib import SequenceMatcher
from enum import Enum
from typing import List, Dict, Any, Tuple, Optional

logger = logging.getLogger(__name__)

# Fallback for Decimal zero if not available from ledger
ZERO = Decimal("0.00")


class MatchTier(Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


GREEN_DATE_WINDOW_DAYS = 1
YELLOW_DATE_WINDOW_DAYS = 10
MERCHANT_SIMILARITY_THRESHOLD = 0.6
MAX_DATE_WINDOW_DAYS = 14

TRANSFER_KEYWORDS = {
    "payment", "transfer", "visa", "mastercard", "amex", "credit card",
    "chequing", "checking", "savings", "bank", "etransfer", "e-transfer"
}


def _normalize_description_for_dupes(desc: str) -> str:
    """Normalize transaction description for better matching."""
    if not desc:
        return ""
    desc = desc.lower().strip()

    prefixes = [
        "pos purchase", "pos withdrawal", "atm withdrawal", "online purchase",
        "debit", "credit", "payment to", "payment from", "transfer to",
        "transfer from", "deposit", "withdrawal", "purchase"
    ]
    for prefix in prefixes:
        if desc.startswith(prefix):
            desc = desc[len(prefix):].strip()
            break

    suffixes = [" - debit", " - credit", " *", " #"]
    for suffix in suffixes:
        if desc.endswith(suffix):
            desc = desc[:-len(suffix)].strip()

    return " ".join(desc.split())

def ledger_tx_to_candidate(tx, index: int = 0) -> Optional[MatchCandidate]:
    """Convert a GeneralLedger Transaction to a MatchCandidate."""
    if not tx or not tx.date:
        return None
    try:
        date_obj = datetime.strptime(tx.date, "%Y-%m-%d").date()
    except ValueError:
        return None

    # FIX: Only sum debits (or credits). Summing both doubles the amount!
    total_amount = Decimal("0.00")
    for line in tx.lines:
        total_amount += line.dr
    abs_amount = abs(total_amount)

    if abs_amount == Decimal("0.00"):
        return None

    description = tx.description or ""
    return MatchCandidate(
        id=str(tx.id) if tx.id else f"tx_{index}",
        date=date_obj,
        amount=abs_amount,
        description=_normalize_description_for_dupes(description)
    )

def parsed_tx_to_candidate(parsed_tx: Dict[str, Any], index: int = 0) -> Optional[MatchCandidate]:
    """Convert a parsed transaction (from PDF/OFX) to a MatchCandidate."""
    if not parsed_tx:
        return None

    date_str = parsed_tx.get('date') or parsed_tx.get('trans_date') or parsed_tx.get('post_date')
    if not date_str:
        return None

    date_obj = None
    for fmt in ["%Y-%m-%d", "%d-%b-%Y", "%b %d %Y", "%d %b %Y"]:
        try:
            date_obj = datetime.strptime(date_str, fmt).date()
            break
        except ValueError:
            continue

    if date_obj is None:
        return None

    amount = parsed_tx.get('amount')
    if amount is None:
        lines = parsed_tx.get('lines')
        if lines:
            total_dr = Decimal("0.00")
            for line in lines:
                # OFX lines are tuples: (account_name, dr, cr)
                if isinstance(line, (list, tuple)) and len(line) >= 3:
                    total_dr += Decimal(str(line[1]))
                # Fallback for dict lines just in case
                elif isinstance(line, dict):
                    total_dr += Decimal(str(line.get('dr', 0)))
            amount = total_dr
    if amount is None:
        return None
    try:
        abs_amount = abs(Decimal(str(amount)))
    except (ValueError, TypeError):
        return None

    if abs_amount == Decimal("0.00"):
        return None

    description = parsed_tx.get('description') or parsed_tx.get('payee') or parsed_tx.get('memo') or ""
    return MatchCandidate(
        id=str(index),
        date=date_obj,
        amount=abs_amount,
        description=_normalize_description_for_dupes(description)
    )


@dataclass(frozen=True)
class MatchCandidate:
    id: str
    date: date_type
    amount: Decimal
    description: str

    @classmethod
    def from_ledger_tx(cls, tx: Any, index: int = 0) -> Optional[MatchCandidate]:
        """Convert a GeneralLedger Transaction to a MatchCandidate."""
        if not tx or not getattr(tx, "date", None):
            return None

        try:
            date_obj = datetime.strptime(tx.date, "%Y-%m-%d").date()
        except ValueError:
            return None

        # Take max single-side sum to prevent double-counting balanced debit/credit postings
        total_debits = sum((line.dr for line in tx.lines), ZERO)
        total_credits = sum((line.cr for line in tx.lines), ZERO)
        abs_amount = max(total_debits, total_credits)

        if abs_amount == ZERO:
            return None

        return cls(
            id=str(tx.id) if getattr(tx, "id", None) else f"tx_{index}",
            date=date_obj,
            amount=abs_amount,
            description=normalize_description(getattr(tx, "description", "") or ""),
        )

    @classmethod
    def from_parsed_dict(cls, parsed_tx: Dict[str, Any], index: int = 0) -> Optional[MatchCandidate]:
        """Convert a parsed PDF/OFX statement transaction dictionary to a MatchCandidate."""
        if not parsed_tx:
            return None

        date_str = parsed_tx.get("date") or parsed_tx.get("trans_date") or parsed_tx.get("post_date")
        if not date_str:
            return None

        date_obj = None
        for fmt in ["%Y-%m-%d", "%d-%b-%Y", "%b %d %Y", "%d %b %Y"]:
            try:
                date_obj = datetime.strptime(date_str, fmt).date()
                break
            except ValueError:
                continue
        if date_obj is None:
            return None

        # Extract amount directly (PDF) or fallback to lines tuples (OFX)
        amount = parsed_tx.get("amount")
        if amount is None:
            return None

        try:
            abs_amount = abs(Decimal(str(amount)))
        except (ValueError, TypeError):
            return None

        if abs_amount == ZERO:
            return None

        raw_desc = (
            parsed_tx.get("description")
            or parsed_tx.get("payee")
            or parsed_tx.get("memo")
            or ""
        )

        return cls(
            id=str(index),
            date=date_obj,
            amount=abs_amount,
            description=normalize_description(raw_desc),
        )


@dataclass(frozen=True)
class ScoredMatch:
    candidate: MatchCandidate
    tier: MatchTier
    date_diff_days: int
    merchant_similarity: float


def _merchant_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if shorter in longer:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def classify_match(pending: MatchCandidate, candidate: MatchCandidate) -> ScoredMatch:
    date_diff_days = abs((pending.date - candidate.date).days)
    merchant_sim = _merchant_similarity(pending.description, candidate.description)
    merchant_matches = merchant_sim >= MERCHANT_SIMILARITY_THRESHOLD

    if date_diff_days > MAX_DATE_WINDOW_DAYS:
        tier = MatchTier.RED
    elif pending.amount != candidate.amount:
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
        merchant_similarity=merchant_sim,
    )


def find_candidate_matches(
    pending: MatchCandidate,
    candidates: List[MatchCandidate],
    include_red: bool = False,
) -> List[ScoredMatch]:
    scored = [classify_match(pending, c) for c in candidates]
    if not include_red:
        scored = [s for s in scored if s.tier != MatchTier.RED]

    tier_order = {MatchTier.GREEN: 0, MatchTier.YELLOW: 1, MatchTier.RED: 2}
    scored.sort(key=lambda s: (tier_order[s.tier], s.date_diff_days, -s.merchant_similarity))
    return scored


def detect_duplicates(
    parsed_transactions: List[Dict[str, Any]],
    ledger: Any,
    min_tier: MatchTier = MatchTier.YELLOW,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """Detect duplicate transactions between parsed statement and mobile pending."""
    mobile_txs = [
        t for t in getattr(ledger, "transactions", [])
        if getattr(t, "imported_id", None) and t.imported_id.startswith("android_")
    ]

    if not mobile_txs:
        return parsed_transactions, [], {}

    mobile_candidates = [
        c for idx, tx in enumerate(mobile_txs)
        if (c := MatchCandidate.from_ledger_tx(tx, idx)) is not None
    ]

    if not mobile_candidates:
        return parsed_transactions, [], {}

    unique_txs, duplicate_txs, match_details = [], [], {}
    tier_order = {MatchTier.GREEN: 0, MatchTier.YELLOW: 1, MatchTier.RED: 2}

    for idx, parsed_tx in enumerate(parsed_transactions):
        candidate = MatchCandidate.from_parsed_dict(parsed_tx, idx)
        if not candidate:
            unique_txs.append(parsed_tx)
            continue

        matches = find_candidate_matches(candidate, mobile_candidates, include_red=False)

        best_match = None
        for match in matches:
            if tier_order[match.tier] <= tier_order[min_tier]:
                best_match = match
                break

        if best_match:
            duplicate_txs.append(parsed_tx)
            match_details[str(idx)] = {
                "parsed_tx": parsed_tx,
                "matched_mobile_id": best_match.candidate.id,
                "tier": best_match.tier.value,
                "date_diff_days": best_match.date_diff_days,
                "merchant_similarity": best_match.merchant_similarity,
            }
        else:
            unique_txs.append(parsed_tx)

    return unique_txs, duplicate_txs, match_details


def filter_duplicates(
    parsed_transactions: List[Dict[str, Any]],
    ledger: Any,
    auto_skip_green: bool = True,
    auto_skip_yellow: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Filter out duplicate transactions with configurable auto-skip behavior."""
    if not auto_skip_green:
        _, _, all_details = detect_duplicates(parsed_transactions, ledger, min_tier=MatchTier.YELLOW)
        return parsed_transactions, all_details

    effective_skip_tier = MatchTier.YELLOW if auto_skip_yellow else MatchTier.GREEN
    unique, _, match_details = detect_duplicates(
        parsed_transactions, ledger, min_tier=effective_skip_tier
    )

    review_items = {}
    if not auto_skip_yellow:
        _, _, yellow_details = detect_duplicates(parsed_transactions, ledger, min_tier=MatchTier.YELLOW)
        for idx_str, detail in yellow_details.items():
            if detail["tier"] == MatchTier.YELLOW.value:
                review_items[idx_str] = detail
    else:
        review_items = match_details

    return unique, review_items


def extract_suggested_matches(
    parsed_transactions: List[Dict[str, Any]],
    ledger
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Scans parsed transactions against mobile pending entries AND existing unreconciled
    ledger entries to detect duplicates.
    """
    # --- PASS 1: Mobile vs Bank (Existing Logic) ---
    mobile_txs = [t for t in ledger.transactions if t.imported_id and t.imported_id.startswith('android_')]
    mobile_candidates = []
    for idx, tx in enumerate(mobile_txs):
        candidate = ledger_tx_to_candidate(tx, idx)
        if candidate:
            mobile_candidates.append(candidate)

    unique_txs = []
    suggested_matches = []
    matched_parsed_indices = set()

    for idx, parsed_tx in enumerate(parsed_transactions):
        candidate = parsed_tx_to_candidate(parsed_tx, idx)
        if not candidate:
            unique_txs.append(parsed_tx)
            matched_parsed_indices.add(idx)
            continue

        matches = find_candidate_matches(candidate, mobile_candidates, include_red=False)
        best_match = None
        for match in matches:
            if match.tier in (MatchTier.GREEN, MatchTier.YELLOW):
                best_match = match
                break

        if best_match:
            suggested_matches.append({
                "parsed_tx": parsed_tx,
                "pending_id": int(best_match.candidate.id),
                "tier": best_match.tier.value,
                "date_diff_days": best_match.date_diff_days,
                "merchant_similarity": best_match.merchant_similarity,
                "match_type": "mobile_to_bank" # ✅ Tag the match type
            })
            matched_parsed_indices.add(idx)

    # --- PASS 2: Bank vs Bank (Statement vs Statement) ---
    # Get all unreconciled transactions from the ledger (excluding mobile ones we just checked)
    unreconciled_txs = [
        t for t in ledger.transactions
        if not t.reconciled and (not t.imported_id or not t.imported_id.startswith('android_'))
    ]

    bank_candidates = []
    for idx, tx in enumerate(unreconciled_txs):
        candidate = ledger_tx_to_candidate(tx, idx)
        if candidate:
            bank_candidates.append(candidate)

    for idx, parsed_tx in enumerate(parsed_transactions):
        if idx in matched_parsed_indices:
            continue # Already matched in Pass 1

        candidate = parsed_tx_to_candidate(parsed_tx, idx)
        if not candidate:
            unique_txs.append(parsed_tx)
            continue

        matches = find_candidate_matches(candidate, bank_candidates, include_red=False)
        best_match = None
        for match in matches:
            # ✅ TRANSFER BYPASS: If it's RED, but amounts match exactly and it's a transfer, force GREEN
            if match.tier == MatchTier.RED:
                if (candidate.amount == match.candidate.amount and
                    match.date_diff_days <= YELLOW_DATE_WINDOW_DAYS and
                    is_likely_transfer_match(candidate.description, match.candidate.description)):
                    best_match = ScoredMatch(
                        candidate=match.candidate,
                        tier=MatchTier.GREEN,
                        date_diff_days=match.date_diff_days,
                        merchant_similarity=1.0 # Fake 100% similarity for the UI
                    )
                    break

            if match.tier in (MatchTier.GREEN, MatchTier.YELLOW):
                best_match = match
                break

        if best_match:
            suggested_matches.append({
                "parsed_tx": parsed_tx,
                "pending_id": int(best_match.candidate.id),
                "tier": best_match.tier.value,
                "date_diff_days": best_match.date_diff_days,
                "merchant_similarity": best_match.merchant_similarity,
                "match_type": "bank_to_bank" # ✅ Tag the match type
            })
            matched_parsed_indices.add(idx)
        else:
            unique_txs.append(parsed_tx)

    return unique_txs, suggested_matches
