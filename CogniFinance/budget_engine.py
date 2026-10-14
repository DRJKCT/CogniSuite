# budget_engine.py
"""
Hybrid Budget Engine (ZBB + Envelope) for CogniFinance.
Derivation and rollover math for the envelope budgeting system.
"""
from decimal import Decimal
from typing import List, Dict, Tuple, Optional
from datetime import datetime
from ledger import GeneralLedger, ZERO, money
from calendar import monthrange


def derive_expected_income(ledger: GeneralLedger, target_month: str, source_type: str) -> Tuple[Decimal, str]:
    """
    Derive expected income for a month based on historical data.
    
    Returns (amount, human_readable_label).
    
    'last_month' -> get_total_revenue_for_month(prev_month)
    'avg_3mo'    -> get_revenue_for_months(last 3) / 3
    'avg_6mo'    -> get_revenue_for_months(last 6) / 6
    'manual'     -> (ZERO, "Manual override")
    """
    if source_type == 'manual':
        return ZERO, "Manual override"
    
    # Parse target month
    try:
        year, month = map(int, target_month.split('-'))
    except (ValueError, AttributeError):
        return ZERO, "Invalid month format"
    
    # Generate previous months
    prev_months = []
    for i in range(1, 7):
        m = month - i
        y = year
        while m <= 0:
            m += 12
            y -= 1
        prev_months.append(f"{y:04d}-{m:02d}")
    
    if source_type == 'last_month':
        months_to_check = prev_months[:1]
        label = "based on Last Month actuals"
    elif source_type == 'avg_3mo':
        months_to_check = prev_months[:3]
        label = "based on 3-Month average"
    elif source_type == 'avg_6mo':
        months_to_check = prev_months[:6]
        label = "based on 6-Month average"
    else:
        return ZERO, "Invalid source type"
    
    total = ledger.get_revenue_for_months(months_to_check)
    
    if total == ZERO:
        return ZERO, "No historical data available"
    
    if source_type == 'last_month':
        return money(total), label
    else:
        divisor = len(months_to_check)
        return money(total / divisor), label


def _get_all_months_from_first_activity(ledger: GeneralLedger, category: str, as_of_month: str) -> List[str]:
    """Get all months from first budget activity through as_of_month."""
    rows = ledger.get_budget_ledger_rows_for_category(category)
    if not rows:
        return [as_of_month]
    
    first_month = rows[0]["month"]
    
    # Generate all months from first_month to as_of_month
    months = []
    try:
        start_year, start_month = map(int, first_month.split('-'))
        end_year, end_month = map(int, as_of_month.split('-'))
    except (ValueError, AttributeError):
        return [as_of_month]
    
    current_year, current_month = start_year, start_month
    while (current_year, current_month) <= (end_year, end_month):
        months.append(f"{current_year:04d}-{current_month:02d}")
        current_month += 1
        if current_month > 12:
            current_month = 1
            current_year += 1
    
    return months


def _net_assignment_from_rows(rows: List[dict]) -> Decimal:
    """Aggregate B: Net Assignment for a category in a single month."""
    total = ZERO
    for row in rows:
        event_type = row["event_type"]
        amount = row["amount"]
        if event_type == 'assign':
            total += amount
        elif event_type == 'unassign':
            total -= amount
        elif event_type == 'move_in':
            total += amount
        elif event_type == 'move_out':
            total -= amount
    return money(total)


def envelope_available(ledger: GeneralLedger, category: str, as_of_month: str) -> Decimal:
    """
    Walk month-by-month from the envelope's first activity through as_of_month,
    applying the rollover policy at each month boundary.
    
    Returns a Decimal. May be negative for carry_forward envelopes.
    """
    # Get envelope info
    envelopes = ledger.get_budget_envelopes(include_inactive=True)
    envelope = None
    for env in envelopes:
        if env["category_name"] == category:
            envelope = env
            break
    
    if not envelope:
        return ZERO
    
    policy = envelope["rollover_policy"]
    linked = envelope["linked_coa_account"]
    
    # Get all rows for this category
    rows = ledger.get_budget_ledger_rows_for_category(category)
    
    # Group by month
    rows_by_month: Dict[str, List[dict]] = {}
    for row in rows:
        m = row["month"]
        if m not in rows_by_month:
            rows_by_month[m] = []
        rows_by_month[m].append(row)
    
    # Get all months from first activity through as_of_month
    months = _get_all_months_from_first_activity(ledger, category, as_of_month)
    
    available = ZERO
    
    for m in months:
        month_rows = rows_by_month.get(m, [])
        assigned = _net_assignment_from_rows(month_rows)  # Aggregate B
        spent = ledger.get_category_actuals(linked, m) if linked else ZERO
        
        if policy == "reset_monthly":
            available = assigned - spent
        elif policy == "carry_forward":
            available = available + assigned - spent
        else:
            raise ValueError(f"Unknown rollover_policy: {policy}")
    
    return money(available)


def ready_to_assign(ledger: GeneralLedger, month: str) -> Decimal:
    """
    Aggregate A: Ready to Assign for a month.
    = Income Received - SUM(assign) + SUM(unassign)
    Moves are excluded.
    """
    income_received = ledger.get_total_revenue_for_month(month)
    
    rows = ledger.get_budget_ledger_rows(month)
    
    assigned = ZERO
    unassigned = ZERO
    
    for row in rows:
        if row["event_type"] == 'assign':
            assigned += row["amount"]
        elif row["event_type"] == 'unassign':
            unassigned += row["amount"]
    
    rta = income_received - assigned + unassigned
    return money(rta)


def net_assignment_in_month(ledger: GeneralLedger, category: str, month: str) -> Decimal:
    """Aggregate B: Net Assignment for a category in a single month."""
    rows = ledger.get_budget_ledger_rows(month)
    category_rows = [r for r in rows if r["category_name"] == category]
    return _net_assignment_from_rows(category_rows)