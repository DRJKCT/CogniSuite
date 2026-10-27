"""
simple_inputs.py ver 0.1
For CogniFinance ver 1.9

Input parsing shared by the Simple Mode forms.
"""
from decimal import Decimal, InvalidOperation

from ledger import ZERO, money


def parse_amount(text: str) -> Decimal:
    """A positive amount rounded to cents. Raises ValueError with a message
    fit to show the user."""
    try:
        value = money(Decimal(text.replace(",", "").replace("$", "").strip()))
    except (InvalidOperation, ValueError):
        raise ValueError("Amount must be a number.")
    if value <= ZERO:
        raise ValueError("Amount must be more than zero.")
    return value
