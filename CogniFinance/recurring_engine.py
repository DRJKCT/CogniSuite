# recurring_engine.py
"""
recurring_engine.py
Pure, deterministic recurring-charge detection for CogniFinance.

The detection counterpart to merchant_normalization.py: it takes plain
data and returns plain data. No PyQt6, no ledger import, no database,
no side effects -- which is what makes it testable against nothing but
a list of dicts (see test/test_recurring_engine.py).

What it does
------------
Given a set of posted charges, find the ones that recur on a regular
cadence and describe each one well enough for a consumer to act on:

    merchant_key / merchant_display  -- who
    category                        -- what it is for
    interval                        -- weekly | monthly | quarterly | annual
    expected_amount                 -- mean of the observed amounts
    amount_variance                 -- population stdev of the observed amounts
    expected_next_date              -- when it should next land
    occurrences                     -- how many charges back the claim
    confidence                      -- how much to trust the claim

Design notes worth knowing before changing anything here
--------------------------------------------------------
1. Detection never writes. It proposes; the ledger stages a proposal as
   a draft, and a human promotes it to a pattern. That mirrors Merchant
   Memory's draft -> confirm -> pattern lifecycle, and it is why this
   module can afford to be aggressive: anything it proposes can still
   be thrown away by the user.

2. Cadence is calendar-aware, not day-counted. Month differences are
   exact integers, so "monthly" means every month-gap is exactly 1,
   "quarterly" means every month-gap is exactly 3, and so on. A fixed
   30-day rule would misfile a charge that lands on the 31st (and then
   on the 28th) as irregular.

3. Amount stability is a soft factor, never a gate.
   amount_stability = 1 / (1 + CV) is exactly 1.0 for a fixed-amount
   subscription -- so subscriptions keep full precision -- while a
   variable utility bill still scores well enough to surface, and
   reports a wider amount_variance so a consumer can judge it.

4. Known limits, stated rather than hidden:
   - Only the four intervals in INTERVALS are recognised. Biweekly and
     "every 90 days" are not detected; they are a data-model addition,
     not a logic change, because interval is a named string everywhere.
   - Revenue-side recurrence is not detected here. The engine would not
     care (it works on (category, amount) pairs), but the ledger
     adapter deliberately feeds it expense charges only.
"""
import calendar
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --- Recognised cadences -------------------------------------------------
INTERVALS = ("weekly", "monthly", "quarterly", "annual")

# Nominal step, in the interval's own natural unit. Weeks for 'weekly',
# calendar months for the rest.
INTERVAL_STEP = {
    "weekly": 1,      # in weeks
    "monthly": 1,     # in months
    "quarterly": 3,   # in months
    "annual": 12,     # in months
}

# Day-of-month tolerance for the calendar intervals. A charge that lands
# on the 15th and then on the 17th (a weekend shift) is still "the 15th".
DAY_OF_MONTH_TOLERANCE = 2

# Minimum charges before a cadence can be called a cadence at all.
MIN_OCCURRENCES = 3

# Occurrences at which the volume factor saturates to 1.0. Lower than
# Merchant Memory's 10 because a recurring charge is a sparse signal by
# nature -- a monthly subscription accrues one observation a month.
VOLUME_CAP = 6

# Below this, a candidate is not worth surfacing for review.
SUGGEST_THRESHOLD = 0.30

# At or above this, the claim is solid enough for a consumer to act on.
PROMOTE_THRESHOLD = 0.60

# Same table as Merchant Memory's rating modifiers, so a Green vouch
# means the same thing in both subsystems.
RATING_MODIFIERS = {"green": 1.2, "yellow": 1.0}

CENT = Decimal("0.01")


# ---------------------------------------------------------------------------
# Money helpers
# ---------------------------------------------------------------------------
# Local rather than imported from ledger.py: this module must stay
# importable without pulling in sqlcipher3, so that the engine can be
# tested (and reused server-side) against plain data alone. The rounding
# rule is deliberately identical to ledger.money().

def _q2(value) -> Decimal:
    """Quantize to cents, half-up -- the same rounding the ledger uses."""
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def _mean(values: Sequence[Decimal]) -> Decimal:
    return _q2(sum(values) / Decimal(len(values)))


def _population_stdev(values: Sequence[Decimal]) -> Decimal:
    if len(values) < 2:
        return Decimal("0.00")
    mean = sum(values) / Decimal(len(values))
    variance = sum((v - mean) ** 2 for v in values) / Decimal(len(values))
    return _q2(variance.sqrt())


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _parse(value: str) -> Optional[date]:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def add_interval(start: str, interval: str, steps: int = 1) -> Optional[str]:
    """
    Advance a date by N intervals, calendar-aware.

    Monthly-ish intervals add calendar months and clamp the day to the
    target month's length, so a charge on the 31st rolls to the 28th
    (or 29th) in February rather than overflowing into March.
    """
    d = _parse(start)
    if d is None:
        return None

    if interval == "weekly":
        return (d + timedelta(weeks=steps)).isoformat()

    step = INTERVAL_STEP.get(interval)
    if step is None:
        return None

    total = (d.year * 12 + (d.month - 1)) + step * steps
    year, month = divmod(total, 12)
    month += 1
    if year < 1 or year > 9999:
        return None
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day).isoformat()


# ---------------------------------------------------------------------------
# Cadence fitting
# ---------------------------------------------------------------------------

def _month_gap(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def _cadence_purity(dates: Sequence[date], interval: str) -> float:
    """Fraction of consecutive gaps that match the interval's cadence."""
    if len(dates) < 2:
        return 0.0

    if interval == "weekly":
        # A week is 7 days; allow a one-day shift either way.
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        matches = sum(1 for g in gaps if abs(g - 7) <= 1)
    else:
        step = INTERVAL_STEP[interval]
        gaps = [_month_gap(a, b) for a, b in zip(dates, dates[1:])]
        matches = sum(1 for g in gaps if g == step)

    return matches / len(gaps)


def _anchor_stability(dates: Sequence[date], interval: str) -> float:
    """
    How stable is the charge's position within its period?

    Weekly: the same weekday (within a day of shift).
    Calendar intervals: the same day-of-month (within a couple of days
    of drift), with a month-end charge treated as matching a late-month
    anchor -- otherwise a bill on the 31st would read as irregular the
    moment it lands on the 28th of February.
    """
    if not dates:
        return 0.0

    if interval == "weekly":
        weekdays = [d.isoweekday() for d in dates]
        counts: Dict[int, int] = {}
        for wd in weekdays:
            counts[wd] = counts.get(wd, 0) + 1
        dominant = max(counts, key=counts.get)
        matches = 0
        for wd in weekdays:
            diff = abs(wd - dominant)
            diff = min(diff, 7 - diff)
            if diff <= 1:
                matches += 1
        return matches / len(dates)

    counts = {}
    for d in dates:
        counts[d.day] = counts.get(d.day, 0) + 1
    modal = max(counts, key=counts.get)

    matches = 0
    for d in dates:
        last_day = calendar.monthrange(d.year, d.month)[1]
        if abs(d.day - modal) <= DAY_OF_MONTH_TOLERANCE:
            matches += 1
        elif d.day == last_day and modal >= 28:
            matches += 1
    return matches / len(dates)


def _fit_interval(dates: Sequence[date]) -> Tuple[Optional[str], float]:
    """Best (interval, regularity) fit for one group of charges."""
    best_interval: Optional[str] = None
    best_regularity = 0.0
    for interval in INTERVALS:
        regularity = _cadence_purity(dates, interval) * _anchor_stability(dates, interval)
        if regularity > best_regularity:
            best_interval = interval
            best_regularity = regularity
    return best_interval, best_regularity


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------

def amount_stability(expected_amount: Decimal, amount_variance: Decimal) -> float:
    """
    How predictable the amount is, as a 0..1 multiplier.

    1 / (1 + CV): exactly 1.0 for a fixed-amount charge (no precision
    lost for subscriptions), degrading smoothly as the amount varies
    (a utility bill at 15% CV still scores 0.87). It is deliberately a
    factor and not a gate -- a regular-but-variable charge is still
    recurring, just less predictable.
    """
    if expected_amount <= 0:
        return 0.0
    cv = amount_variance / expected_amount
    return 1.0 / (1.0 + float(cv))


def compute_confidence(interval_regularity: float, amount_stability_factor: float,
                       occurrences: int, rating: str = "yellow") -> float:
    """
    Confidence in a recurring claim.

    Same shape as Merchant Memory's confidence: a product of
    independently normalized factors, with the user's rating applied
    last as a multiplier. Deliberately NOT clamped to 1.0 -- a
    deliberate Green vouch can push a claim past 1.0, exactly as it
    does in Merchant Memory.
    """
    volume_factor = min(1.0, occurrences / VOLUME_CAP) if occurrences > 0 else 0.0
    base = interval_regularity * amount_stability_factor * volume_factor
    modifier = RATING_MODIFIERS.get(rating, 1.0)
    return base * modifier


def compute_declared_confidence(rating: str = "green") -> float:
    """
    Confidence for a user-declared pattern.

    A declared pattern has no transaction evidence to weigh, so the
    declaration itself is the evidence: the claim is treated as exactly
    regular, exactly stable and fully volumed, then rated normally.
    """
    return compute_confidence(1.0, 1.0, VOLUME_CAP, rating)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect_recurring_candidates(charges: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Find recurring patterns in a set of posted charges.

    Each charge is a dict:
        {"date": "YYYY-MM-DD", "merchant_key": str, "merchant_display": str,
         "category": str, "amount": Decimal | str}

    The caller is responsible for merging a split transaction's same-
    category legs into one charge before calling -- the engine counts
    occurrences, not ledger lines.

    Returns the candidates that clear SUGGEST_THRESHOLD, most
    confident first. Pure: it reads the input, never the ledger, and
    never writes anything.
    """
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}

    for charge in charges:
        merchant_key = (charge.get("merchant_key") or "").strip()
        category = (charge.get("category") or "").strip()
        d = _parse(charge.get("date") or "")
        raw_amount = charge.get("amount")

        if not merchant_key or not category or d is None or raw_amount is None:
            continue
        try:
            amount = _q2(raw_amount)
        except Exception:
            continue
        if amount <= 0:
            continue

        groups.setdefault((merchant_key, category), []).append({
            "date": d,
            "amount": amount,
            "merchant_display": charge.get("merchant_display") or merchant_key,
        })

    candidates: List[Dict[str, Any]] = []

    for (merchant_key, category), rows in groups.items():
        if len(rows) < MIN_OCCURRENCES:
            continue

        rows.sort(key=lambda r: r["date"])
        dates = [r["date"] for r in rows]
        amounts = [r["amount"] for r in rows]

        interval, regularity = _fit_interval(dates)
        if interval is None or regularity <= 0.0:
            continue

        expected_amount = _mean(amounts)
        amount_variance = _population_stdev(amounts)
        stability = amount_stability(expected_amount, amount_variance)
        confidence = compute_confidence(regularity, stability, len(rows), "yellow")

        if confidence < SUGGEST_THRESHOLD:
            continue

        candidates.append({
            "merchant_key": merchant_key,
            "merchant_display": rows[0]["merchant_display"],
            "category": category,
            "interval": interval,
            "expected_amount": expected_amount,
            "amount_variance": amount_variance,
            "expected_next_date": add_interval(dates[-1].isoformat(), interval),
            "occurrences": len(rows),
            "interval_regularity": round(regularity, 4),
            "amount_stability": round(stability, 4),
            "confidence": round(confidence, 4),
            "evidence_dates": [d.isoformat() for d in dates],
            "first_seen": dates[0].isoformat(),
            "last_seen": dates[-1].isoformat(),
        })

    candidates.sort(key=lambda c: c["confidence"], reverse=True)
    return candidates
