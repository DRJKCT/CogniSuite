import pytest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime
from parser_integration import (
    _parse_cibc_full,
    _parse_tangerine_full,
    _parse_tangerine_bank_statement_full,
    detect_statement_type_from_text,
    detect_bank_from_text
)
from pdf_import import normalize_statement_date

# ==========================================
# 1. PARSER REGRESSION TESTS
# ==========================================
def test_cibc_parser_basic_extraction():
    """Ensure the CIBC regex correctly extracts date, description, and amount."""
    text = """
    Trans date Post date Description Category Amount
    Jan 05 Jan 06 TIM HORTONS #1234 TORONTO ON GROCERY 5.50
    Jan 06 Jan 07 UBER TRIP HELP.UBER.COM TRAVEL 25.00
    """
    txs, skipped = _parse_cibc_full(text)
    assert len(txs) == 2
    assert txs[0]["amount"] == 5.50
    assert "TIM HORTONS" in txs[0]["description"]
    assert len(skipped) == 0

def test_tangerine_bank_balance_trail_sign_inference():
    """
    THE SUPERPOWER TEST: Ensure the parser uses the running balance
    to correctly infer positive (deposit) vs negative (withdrawal) signs.
    """
    text = """
    Opening Balance                                                    1,000.00
    05 Jan 2026       DEPOSIT EMPLOYER               2,000.00      3,000.00
    06 Jan 2026       TIM HORTONS                    5.00          2,995.00
    Closing Balance                                                    2,995.00
    """
    result = _parse_tangerine_bank_statement_full(text)
    txs = result["transactions"]

    assert len(txs) == 2
    # Deposit: Balance went UP, so amount must be positive
    assert txs[0]["amount"] == 2000.00
    assert txs[0]["balance_check"] == "ok"

    # Withdrawal: Balance went DOWN, so amount must be negative
    assert txs[1]["amount"] == -5.00
    assert txs[1]["balance_check"] == "ok"

def test_tangerine_bank_balance_mismatch_detection():
    """Ensure the parser flags a transaction if the math doesn't add up."""
    text = """
    Opening Balance                                                    1,000.00
    05 Jan 2026       TIM HORTONS                    5.00          990.00
    """
    result = _parse_tangerine_bank_statement_full(text)
    txs = result["transactions"]

    # Delta is -10.00, but parsed amount is 5.00. The parser MUST flag this!
    assert txs[0]["balance_check"] == "mismatch"

# ==========================================
# 2. TEMPORAL & BOUNDARY TESTS
# ==========================================
def test_date_inference_year_end_boundary():
    """Ensure December transactions aren't mislabeled as the new year."""
    ref = datetime(2026, 1, 10)

    # "Dec 28" is >60 days away from Jan 10, so it must belong to the previous year
    assert normalize_statement_date("Dec 28", ref) == "2025-12-28"

    # "Jan 05" is close to Jan 10, so it belongs to the current year
    assert normalize_statement_date("Jan 05", ref) == "2026-01-05"

def test_statement_type_detection():
    bank_text = "Opening Balance Closing Balance Chequing Account"
    card_text = "Credit Card Statement Minimum Payment Due"

    assert detect_statement_type_from_text(bank_text) == "bank"
    assert detect_statement_type_from_text(card_text) == "credit_card"
    assert detect_bank_from_text("Tangerine Bank") == "tangerine"
