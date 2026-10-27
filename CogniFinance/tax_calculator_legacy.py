"""
tax_calculator.py ver 0.2
For CogniFinanace ver 1.95
Dynamic tax calculation using JSON templates from tax_templates/ folder.
"""
import os
import json
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List  # <--- FIXED: Added List to imports
from pathlib import Path
from config import TAX_TEMPLATES_DIR

ZERO = Decimal("0.00")
HUNDRED = Decimal("100.00")

def _quantize(val: Decimal) -> Decimal:
    return val.quantize(ZERO, rounding=ROUND_HALF_UP)

def _calc_bracket_tax(income: Decimal, brackets: list) -> Decimal:
    """Calculates tax using progressive marginal brackets."""
    tax = ZERO
    prev_threshold = ZERO
    for bracket in brackets:
        threshold = Decimal(str(bracket["threshold"]))
        rate = Decimal(str(bracket["rate"]))
        if income <= prev_threshold:
            break
        taxable_in_bracket = min(income, threshold) - prev_threshold
        tax += _quantize(taxable_in_bracket * rate)
        prev_threshold = threshold
    return tax

def _load_tax_template(tax_year: str = "2025_2026_canada_ontario") -> dict:
    """
    Loads tax rates from JSON template in tax_templates/ folder.
    Looks for: tax_templates/{tax_year}.json
    """
    template_path = TAX_TEMPLATES_DIR / f"{tax_year}.json"

    if not template_path.exists():
        raise FileNotFoundError(
            f"Tax template not found: {template_path}\n"
            f"Available templates should be placed in: {TAX_TEMPLATES_DIR / 'tax_templates'}"
        )

    with open(template_path, "r", encoding="utf-8") as f:
        return json.load(f)

def estimate_tax_liability(net_income: Decimal, tax_year: str = "2025_2026_canada_ontario") -> Dict[str, Decimal]:
    """
    Estimates total tax liability for an Ontario-based self-employed
    professional (e.g., Associate Dentist T2125).
    Loads all rates dynamically from tax_templates/{tax_year}.json
    """
    template = _load_tax_template(tax_year)

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
    fed_brackets = template["federal_brackets"]
    fed_gross = _calc_bracket_tax(net_income, fed_brackets)

    # =====================================================================
    # 2. ONTARIO PROVINCIAL TAX BRACKETS (2025/2026)
    # =====================================================================
    prov_brackets = template["provincial_brackets"]
    on_gross = _calc_bracket_tax(net_income, prov_brackets)

    # =====================================================================
    # 3. CPP & CPP2 (Self-Employed = pay both employer & employee portions)
    # =====================================================================
    # 2025/2026 CPP Basic: 11.9% on pensionable earnings between $3,500 and $71,300 [[19]], [[23]]
    cpp_config = template["cpp"]
    cpp_basic_max = Decimal(str(cpp_config["basic_max"]))
    cpp_basic_exempt = Decimal(str(cpp_config["basic_exempt"]))
    cpp_basic_rate = Decimal(str(cpp_config["basic_rate"]))

    cpp_basic = _quantize(min(net_income, cpp_basic_max) - cpp_basic_exempt) * cpp_basic_rate
    if cpp_basic < ZERO:
        cpp_basic = ZERO

    # CPP2 (Additional)
    cpp2_max = Decimal(str(cpp_config["cpp2_max"]))
    cpp2_threshold = Decimal(str(cpp_config["cpp2_threshold"]))
    cpp2_rate = Decimal(str(cpp_config["cpp2_rate"]))

    cpp2 = _quantize(min(net_income, cpp2_max) - cpp2_threshold) * cpp2_rate
    if cpp2 < ZERO:
        cpp2 = ZERO

    total_cpp = _quantize(cpp_basic + cpp2)

    # =====================================================================
    # 4. ONTARIO HEALTH PREMIUM (OHP)
    # =====================================================================
    # Simplified flat brackets reflecting the maximums at each income tier.
    ohp = ZERO
    for tier in template["ontario_health_premium"]:
        if net_income > Decimal(str(tier["min_income"])):
            ohp = Decimal(str(tier["premium"]))
            break

    # =====================================================================
    # 5. BASIC PERSONAL AMOUNT (BPA) CREDITS (2025/2026)
    # =====================================================================
    # Fed 2025 BPA: $16,129 * 14.5% lowest bracket = $2,338.71 [[28]]
    # ON 2025 BPA: $12,399 * 5.05% lowest bracket = $626.15
    credits_config = template["credits"]
    fed_bpa = Decimal(str(credits_config["federal_basic_personal_amount"]))
    fed_rate = Decimal(str(credits_config["federal_lowest_rate"]))
    on_bpa = Decimal(str(credits_config["provincial_basic_personal_amount"]))
    on_rate = Decimal(str(credits_config["provincial_lowest_rate"]))

    fed_credit = _quantize(fed_bpa * fed_rate)
    on_credit = _quantize(on_bpa * on_rate)
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

def get_available_tax_templates() -> List[str]:
    """Returns a list of available tax template filenames."""

    if not TAX_TEMPLATES_DIR.exists():
        return []

    valid_templates = []
    for filepath in TAX_TEMPLATES_DIR.glob("*.json"):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "federal_brackets" in data:
                    valid_templates.append(filepath.stem)
        except Exception:
            continue

    return valid_templates
