# currency.py v1.0
# Multi-currency primitives for CogniFinance (phase 1/2 foundation).
#
# Pure helpers only -- no database access here. The ledger owns storage
# (sql_fx_rates, sql_ledger_meta) and calls into this module for precision
# rules, rate math and the FxInfo record that rides on a transaction line.
#
# Conventions used everywhere:
#   * A rate is "base units per 1 unit of the foreign currency".
#     1 USD = 1.37 CAD (base) -> rate = 1.37
#   * Base-currency amounts are always held to 2 decimals (the ledger's
#     money()). Native (foreign) amounts are held to that currency's own
#     minor-unit precision (JPY 0, USD 2, KWD 3, ...).
#   * Rates are held to RATE_DECIMALS places.

import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

DEFAULT_BASE_CURRENCY = "CAD"   # one place to change; the app should prompt
                                # for the real base currency on first run via
                                # GeneralLedger.set_base_currency() before the
                                # first transaction is posted.

CENT = Decimal("0.01")
RATE_DECIMALS = 12
RATE_QUANT = Decimal(1).scaleb(-RATE_DECIMALS)

# ISO 4217 minor units. Anything not listed uses 2.
_ZERO_DECIMAL = {
    "BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG",
    "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
}
_THREE_DECIMAL = {"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"}
_FOUR_DECIMAL = {"CLF", "UYW"}

_CODE_RE = re.compile(r"^[A-Z]{3}$")


def normalize_currency(code) -> str:
    """Upper-case, strip and validate a 3-letter ISO-style currency code."""
    if not isinstance(code, str):
        raise ValueError("Currency code must be a string such as 'USD'.")
    c = code.strip().upper()
    if not _CODE_RE.match(c):
        raise ValueError(f"'{code}' is not a valid 3-letter currency code.")
    return c


def currency_decimals(code: str) -> int:
    c = normalize_currency(code)
    if c in _ZERO_DECIMAL:
        return 0
    if c in _THREE_DECIMAL:
        return 3
    if c in _FOUR_DECIMAL:
        return 4
    return 2


def minor_unit(code: str) -> Decimal:
    """Smallest representable amount in a currency (0.01, 1, 0.001, ...)."""
    return Decimal(1).scaleb(-currency_decimals(code))


def quantize_native(amount, code: str) -> Decimal:
    """Round a native amount to its currency's precision (ROUND_HALF_UP)."""
    return Decimal(str(amount)).quantize(minor_unit(code), rounding=ROUND_HALF_UP)


def quantize_rate(rate) -> Decimal:
    return Decimal(str(rate)).quantize(RATE_QUANT, rounding=ROUND_HALF_UP)


def to_base(native, rate) -> Decimal:
    """native x rate, rounded to the ledger's 2-decimal base precision."""
    return (Decimal(str(native)) * Decimal(str(rate))).quantize(CENT, rounding=ROUND_HALF_UP)


def derive_rate(base, native) -> Decimal:
    """Effective rate implied by a base amount and a native amount."""
    native = Decimal(str(native))
    if native == 0:
        raise ValueError("Cannot derive a rate from a zero native amount.")
    return quantize_rate(Decimal(str(base)) / native)


def fx_tolerance(native, rate, code: str) -> Decimal:
    """
    How far a line's stored base amount may sit from native x rate before
    the ledger rejects it. One cent, or one native minor unit's worth of
    base, whichever is larger -- this absorbs rounding in derived rates
    without letting a wrong rate through.
    """
    unit_in_base = minor_unit(code) * Decimal(str(rate))
    rel = abs(Decimal(str(native)) * Decimal(str(rate))) * Decimal("1e-9")
    return max(CENT, unit_in_base, rel)


@dataclass(frozen=True)
class FxInfo:
    """
    Foreign-currency detail for ONE transaction line.

    currency   : ISO code of the account the line posts to.
    native     : positive amount in `currency`, on the same side (dr/cr)
                 as the line's base amount.
    rate       : base units per 1 native unit, as used for THIS line.
    rate_date  : the date the rate is effective (may be earlier than the
                 transaction date if a nearest-prior rate was used).
    rate_source: free text -- "manual", "fetched:<provider>", "implied by
                 transfer", "... (inverse)", etc.
    """
    currency: str
    native: Decimal
    rate: Decimal
    rate_date: str
    rate_source: str = "manual"


@dataclass(frozen=True)
class FxRate:
    """A rate looked up from the rate table."""
    rate: Decimal
    rate_date: str
    source: str
    inverted: bool = False


def parse_decimal(value, what: str = "amount") -> Decimal:
    """Decimal(str(value)) with a friendly error and a finiteness check."""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"Invalid {what}: {value!r}")
    if not d.is_finite():
        raise ValueError(f"Invalid {what}: {value!r}")
    return d
