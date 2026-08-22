# tax_calculator.py
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict

ZERO = Decimal("0.00")
HUNDRED = Decimal("100.00")

def _quantize(val: Decimal) -> Decimal:
    return val.quantize(ZERO, rounding=ROUND_HALF_UP)

def _calc_bracket_tax(income: Decimal, brackets: list) -> Decimal:
    """Calculates tax using progressive marginal brackets."""
    tax = ZERO
    prev_threshold = ZERO
    for threshold, rate in brackets:
        if income <= prev_threshold:
            break
        taxable_in_bracket = min(income, threshold) - prev_threshold
        tax += _quantize(taxable_in_bracket * rate)
        prev_threshold = threshold
    return tax

def estimate_tax_liability(net_income: Decimal) -> Dict[str, Decimal]:
    """
    Estimates total tax liability for an Ontario-based self-employed
    professional (e.g., Associate Dentist T2125).
    Uses 2023/2024 tax brackets and rates.
    """
    if net_income <= ZERO:
        return {
            "fed_tax": ZERO, "prov_tax": ZERO, "cpp": ZERO,
            "ohp": ZERO, "credits": ZERO, "total_tax": ZERO,
            "effective_rate": ZERO
        }

    # =====================================================================
    # 1. FEDERAL TAX BRACKETS (2025/2026)
    # Note: The lowest federal rate was recently reduced from 15% to 14.5% [[9]].
    # =====================================================================
    fed_brackets = [
        (Decimal("57375"), Decimal("0.145")),    # 14.5% on the first $57,375 [[5]]
        (Decimal("114750"), Decimal("0.205")),   # 20.5% on income over $57,375 up to $114,750 [[5]]
        (Decimal("177882"), Decimal("0.26")),    # 26.0% on income over $114,750 up to $177,882 [[5]]
        (Decimal("253414"), Decimal("0.29")),    # 29.0% on income over $177,882 up to $253,414 [[5]]
        (Decimal("999999999"), Decimal("0.33")), # 33.0% on income over $253,414 [[4]]
    ]
    fed_gross = _calc_bracket_tax(net_income, fed_brackets)

    # =====================================================================
    # 2. ONTARIO PROVINCIAL TAX BRACKETS (2025/2026)
    # =====================================================================
    on_brackets = [
        (Decimal("52886"), Decimal("0.0505")),   # 5.05% on the first $52,886 [[13]]
        (Decimal("105775"), Decimal("0.0915")),  # 9.15% on income over $52,886 up to $105,775 [[13]]
        (Decimal("150000"), Decimal("0.1116")),  # 11.16% on income over $105,775 up to $150,000 [[13]]
        (Decimal("220000"), Decimal("0.1216")),  # 12.16% on income over $150,000 up to $220,000 [[12]]
        (Decimal("999999999"), Decimal("0.1316")), # 13.16% on income over $220,000 [[12]]
    ]
    on_gross = _calc_bracket_tax(net_income, on_brackets)

    # =====================================================================
    # 3. CPP & CPP2 (Self-Employed = pay both employer & employee portions)
    # =====================================================================
    # 2025/2026 CPP Basic: 11.9% on pensionable earnings between $3,500 and $71,300 [[19]], [[23]]
    cpp_basic_max = Decimal("71300")
    cpp_basic_exempt = Decimal("3500")
    cpp_basic_rate = Decimal("0.119")
    cpp_basic = _quantize(min(net_income, cpp_basic_max) - cpp_basic_exempt) * cpp_basic_rate
    if cpp_basic < ZERO: cpp_basic = ZERO

    # 2025/2026 CPP2 (Additional): 8% (4% emp + 4% er) on earnings between $71,300 and $79,500 [[20]], [[25]]
    cpp2_max = Decimal("79500")
    cpp2_threshold = Decimal("71300")
    cpp2_rate = Decimal("0.08")
    cpp2 = _quantize(min(net_income, cpp2_max) - cpp2_threshold) * cpp2_rate
    if cpp2 < ZERO: cpp2 = ZERO

    total_cpp = _quantize(cpp_basic + cpp2)

    # =====================================================================
    # 4. ONTARIO HEALTH PREMIUM (OHP)
    # =====================================================================
    # Simplified flat brackets reflecting the maximums at each income tier.
    ohp = ZERO
    if net_income > Decimal("200000"): ohp = Decimal("900")
    elif net_income > Decimal("150000"): ohp = Decimal("750")
    elif net_income > Decimal("100000"): ohp = Decimal("600")
    elif net_income > Decimal("72000"): ohp = Decimal("450")
    elif net_income > Decimal("48000"): ohp = Decimal("300")

    # =====================================================================
    # 5. BASIC PERSONAL AMOUNT (BPA) CREDITS (2025/2026)
    # =====================================================================
    # Fed 2025 BPA: $16,129 * 14.5% lowest bracket = $2,338.71 [[28]]
    # ON 2025 BPA: $12,399 * 5.05% lowest bracket = $626.15
    fed_credit = _quantize(Decimal("16129") * Decimal("0.145"))
    on_credit = _quantize(Decimal("12399") * Decimal("0.0505"))
    total_credits = fed_credit + on_credit


    # =====================================================================
    # 6. FINAL TOTALS
    # =====================================================================
    fed_net = max(fed_gross - fed_credit, ZERO)
    on_net = max(on_gross - on_credit, ZERO)

    total_tax = _quantize(fed_net + on_net + total_cpp + ohp)
    effective_rate = _quantize((total_tax / net_income) * HUNDRED) if net_income > ZERO else ZERO

    return {
        "fed_tax": fed_net,
        "prov_tax": on_net,
        "cpp": total_cpp,
        "ohp": ohp,
        "credits": total_credits,
        "total_tax": total_tax,
        "effective_rate": effective_rate
    }
