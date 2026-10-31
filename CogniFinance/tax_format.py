# tax_format.py
"""
tax_format.py ver 1.0
For CogniFinance ver 0.9.67

Currency and rounding helpers shared by the tax calculator and the UI.
Pure Python (no Qt): the UI passes in its locale's separators.

Why this exists: the calculator used to round everything half-up to cents
and the UI hard-coded "$" and two decimals. Neither holds globally --
JPY has no minor unit, KWD has three decimals, and several countries
truncate or round payable tax to whole currency units.
"""
from __future__ import annotations

from decimal import (
    Decimal, ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, ROUND_UP,
)
from typing import Optional

# Template `rounding.mode` -> Decimal rounding constant.
# "down" / "up" are toward / away from zero.
ROUNDING_MODES = {
    "half_up": ROUND_HALF_UP,
    "half_even": ROUND_HALF_EVEN,
    "down": ROUND_DOWN,
    "up": ROUND_UP,
}

# ISO 4217 minor units for the currencies that are not 2. Anything not
# listed is treated as 2 decimals.
_ZERO_DECIMAL = {
    "JPY", "KRW", "VND", "CLP", "ISK", "UGX", "PYG", "RWF", "DJF",
    "GNF", "KMF", "VUV", "XAF", "XOF", "XPF",
}
_THREE_DECIMAL = {"BHD", "JOD", "KWD", "OMR", "TND", "LYD", "IQD"}

_SYMBOLS = {
    "USD": "$", "CAD": "$", "AUD": "$", "NZD": "$", "SGD": "$", "HKD": "$",
    "EUR": "\u20ac", "GBP": "\u00a3", "JPY": "\u00a5", "CNY": "\u00a5",
    "INR": "\u20b9", "KRW": "\u20a9",
}


def currency_minor_units(code: Optional[str]) -> int:
    """Number of decimal places for a currency (2 when unknown/None)."""
    if not code:
        return 2
    code = code.upper()
    if code in _ZERO_DECIMAL:
        return 0
    if code in _THREE_DECIMAL:
        return 3
    return 2


def round_to_increment(value: Decimal, increment: Decimal, mode: str) -> Decimal:
    """Round value to a multiple of increment (e.g. 0.01, 1, 10) using mode."""
    steps = (value / increment).quantize(Decimal(1), rounding=ROUNDING_MODES[mode])
    return steps * increment


def format_money(
    amount,
    currency: Optional[str] = None,
    decimal_point: str = ".",
    group_separator: str = ",",
) -> str:
    """
    Format an amount for display: currency-appropriate decimals, caller-
    supplied separators (pass QLocale's), a symbol where we know one and
    the ISO code otherwise. currency=None keeps the historical "$" look.
    Groups by thousands; regional grouping styles (e.g. lakh) are not
    handled.
    """
    places = currency_minor_units(currency)
    quantum = Decimal(1).scaleb(-places)
    amt = Decimal(str(amount)).quantize(quantum, rounding=ROUND_HALF_UP)
    negative = amt < 0
    whole, _, frac = f"{abs(amt):.{places}f}".partition(".")

    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    body = group_separator.join(groups)
    if places:
        body += decimal_point + frac

    if currency:
        code = currency.upper()
        symbol = _SYMBOLS.get(code, f"{code}\u00a0")
    else:
        symbol = "$"
    return f"{'-' if negative else ''}{symbol}{body}"
