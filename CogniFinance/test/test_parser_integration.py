"""
Unit tests for parser_integration.py.

These test the pure text-parsing functions (parse_*_text) directly with
plain strings, rather than real PDF files. That's deliberate: the
PDF-reading wrappers (parse_*_pdf) are thin - they just extract text via
pdfplumber and hand it to the corresponding *_text function - so testing
the text-parsing logic in isolation covers the actual parsing behavior
without needing binary PDF fixtures, and runs fast with no dependency on
pdfplumber being able to open a file.

Run with:
    python -m unittest discover -s tests
or, if pytest is installed:
    pytest tests/
"""
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser_integration import (
    clean_amount,
    detect_statement_type_from_text,
    detect_bank_from_text,
    parse_cibc_text,
    _parse_cibc_full,
    parse_tangerine_text,
    _parse_tangerine_full,
    parse_tangerine_bank_statement_text,
    _parse_tangerine_bank_statement_full,
    raw_rows_to_import_lines,
)


class FakeLedger:
    """Minimal stand-in for GeneralLedger.apply_category_rules, so the
    row -> import-line logic can be tested without a real database."""

    def __init__(self, rules: dict | None = None):
        self.rules = rules or {}

    def apply_category_rules(self, description: str):
        desc = description.lower()
        for keyword, category in self.rules.items():
            if keyword.lower() in desc:
                return category
        return None


class TestCleanAmount(unittest.TestCase):
    def test_plain_amount(self):
        self.assertEqual(clean_amount("45.67"), 45.67)

    def test_dollar_sign_and_commas(self):
        self.assertEqual(clean_amount("$1,234.56"), 1234.56)

    def test_parentheses_mean_negative(self):
        self.assertEqual(clean_amount("(45.67)"), -45.67)

    def test_leading_minus_is_negative(self):
        self.assertEqual(clean_amount("-45.67"), -45.67)

    def test_none_returns_none(self):
        self.assertIsNone(clean_amount(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(clean_amount(""))
        self.assertIsNone(clean_amount("   "))

    def test_garbage_returns_none(self):
        self.assertIsNone(clean_amount("not a number"))

    def test_stray_dollar_sign_stripped_regardless_of_position(self):
        # clean_amount should tolerate a $ whether or not it's "supposed"
        # to be there -- this matters because parse_tangerine_text passes
        # amount strings through that may or may not already include one.
        self.assertEqual(clean_amount("$45.67"), 45.67)
        self.assertEqual(clean_amount("45.67"), 45.67)


class TestDetectStatementType(unittest.TestCase):
    def test_bank_markers_detected(self):
        text = "Transaction Date Transaction Description Amount($) Balance($) Opening Balance"
        self.assertEqual(detect_statement_type_from_text(text), "bank")

    def test_credit_card_markers_detected(self):
        text = "Credit Card Statement Previous Balance New Balance Minimum Payment Due"
        self.assertEqual(detect_statement_type_from_text(text), "credit_card")

    def test_empty_text_returns_none(self):
        self.assertIsNone(detect_statement_type_from_text(""))
        self.assertIsNone(detect_statement_type_from_text(None))

    def test_no_markers_returns_none(self):
        self.assertIsNone(detect_statement_type_from_text("Hello world, nothing relevant here."))

    def test_tangerine_balance_fallback(self):
        # Tie-breaker: no other markers present, but "balance($)" alone
        # should still be recognized as a bank statement.
        text = "Some heading Balance($) some footer"
        self.assertEqual(detect_statement_type_from_text(text), "bank")


class TestDetectBank(unittest.TestCase):
    def test_cibc_detected(self):
        self.assertEqual(detect_bank_from_text("CIBC Visa Statement"), "cibc")
        self.assertEqual(
            detect_bank_from_text("Canadian Imperial Bank of Commerce"), "cibc"
        )

    def test_tangerine_detected(self):
        self.assertEqual(detect_bank_from_text("Tangerine Bank statement"), "tangerine")

    def test_unknown_bank_returns_none(self):
        self.assertIsNone(detect_bank_from_text("Some Other Bank Inc."))


class TestParseCibcText(unittest.TestCase):
    def test_parses_basic_transaction_line(self):
        text = (
            "Trans date Post date Description Amount\n"
            "Jan 05 Jan 06 AMAZON.CA PURCHASE RETAIL AND GROCERY 45.67\n"
        )
        rows = parse_cibc_text(text)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["bank"], "CIBC")
        self.assertEqual(row["trans_date"], "Jan 05")
        self.assertEqual(row["post_date"], "Jan 06")
        self.assertEqual(row["amount"], 45.67)
        # description/category split is intentionally lazy-matched
        # (documents current, imperfect behavior as a regression guard)
        self.assertEqual(row["description"], "AMAZON.CA")

    def test_skips_header_and_footer_lines(self):
        text = (
            "Trans date Post date Description Amount\n"
            "Card number: 1234 5678 9012 3456\n"
            "Page 1 of 2\n"
        )
        rows = parse_cibc_text(text)
        self.assertEqual(rows, [])

    def test_multiple_transactions(self):
        text = (
            "Jan 05 Jan 06 AMAZON.CA PURCHASE RETAIL AND GROCERY 45.67\n"
            "Feb 12 Feb 13 UBER EATS TORONTO ON RESTAURANTS 23.10\n"
        )
        rows = parse_cibc_text(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["amount"] for r in rows], [45.67, 23.10])

    def test_empty_text_returns_empty_list(self):
        self.assertEqual(parse_cibc_text(""), [])
        self.assertEqual(parse_cibc_text(None), [])

    def test_unmatched_line_is_flagged_for_review(self):
        text = (
            "Jan 05 Jan 06 AMAZON.CA PURCHASE RETAIL AND GROCERY 45.67\n"
            "Some garbled line that isn't a real transaction at all\n"
        )
        transactions, skipped = _parse_cibc_full(text)
        self.assertEqual(len(transactions), 1)
        self.assertEqual(len(skipped), 1)
        self.assertIn("garbled line", skipped[0]["line"])
        self.assertIn("did not match", skipped[0]["reason"])

    def test_header_footer_lines_are_not_flagged(self):
        text = (
            "Trans date Post date Description Amount\n"
            "Card number: 1234 5678 9012 3456\n"
            "Page 1 of 2\n"
        )
        transactions, skipped = _parse_cibc_full(text)
        self.assertEqual(transactions, [])
        self.assertEqual(skipped, [])


class TestParseTangerineText(unittest.TestCase):
    def test_parses_single_line_transaction(self):
        text = (
            "Transaction Date Posted Date Description Category Amount\n"
            "05-Jan-2026 06-Jan-2026 AMAZON.CA PURCHASE 45.67\n"
        )
        rows = parse_tangerine_text(text)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["trans_date"], "05-Jan-2026")
        self.assertEqual(row["post_date"], "06-Jan-2026")
        self.assertEqual(row["description"], "AMAZON.CA PURCHASE")
        self.assertEqual(row["amount"], 45.67)

    def test_parses_description_wrapped_onto_next_line(self):
        # Regression test: the amount column isn't always on the same
        # extracted line as the date/description when pdfplumber wraps
        # long descriptions across lines.
        text = (
            "12-Feb-2026 13-Feb-2026 UBER EATS\n"
            "TORONTO ON 23.10\n"
        )
        rows = parse_tangerine_text(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["description"], "UBER EATS TORONTO ON")
        self.assertEqual(rows[0]["amount"], 23.10)

    def test_amount_without_dollar_sign_is_still_found(self):
        # Regression test for a bug where amounts not literally prefixed
        # with "$" in the extracted text were silently dropped.
        text = "01-Mar-2026 02-Mar-2026 GROCERY STORE 88.42\n"
        rows = parse_tangerine_text(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], 88.42)

    def test_amount_with_dollar_sign_still_works(self):
        text = "01-Mar-2026 02-Mar-2026 GROCERY STORE $88.42\n"
        rows = parse_tangerine_text(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], 88.42)

    def test_header_only_produces_no_rows(self):
        text = "Transaction Date Posted Date Description Category Amount\nPage 1 of 3\n"
        self.assertEqual(parse_tangerine_text(text), [])

    def test_empty_text_returns_empty_list(self):
        self.assertEqual(parse_tangerine_text(""), [])
        self.assertEqual(parse_tangerine_text(None), [])

    def test_dated_line_with_no_amount_is_flagged(self):
        text = "05-Jan-2026 06-Jan-2026 SOME VENDOR WITH NO AMOUNT AT ALL\n"
        transactions, skipped = _parse_tangerine_full(text)
        self.assertEqual(transactions, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("could not locate an amount", skipped[0]["reason"])

    def test_stray_amount_without_date_is_flagged(self):
        # Simulates a wrapped-description continuation line where the
        # amount landed but no date/description context is on this line.
        text = "random trailing text $45.67 with no date pair nearby\n"
        transactions, skipped = _parse_tangerine_full(text)
        self.assertEqual(transactions, [])
        self.assertEqual(len(skipped), 1)

    def test_ordinary_prose_line_is_not_flagged(self):
        # No date, no amount -- almost certainly boilerplate, not worth
        # flagging (avoids burying real issues in noise).
        text = "Thank you for banking with us.\n"
        transactions, skipped = _parse_tangerine_full(text)
        self.assertEqual(transactions, [])
        self.assertEqual(skipped, [])


class TestParseTangerineBankStatementText(unittest.TestCase):
    SAMPLE = (
        "Tangerine\n"
        "Statement Period: 01 Nov 2025 - 30 Nov 2025\n"
        "Transaction Date  Transaction Description              Amount($)  Balance($)\n"
        "Opening Balance                                                    1,000.00\n"
        "05 Nov 2025       INTERAC e-Transfer To: John Smith    200.00      800.00\n"
        "12 Nov 2025       Payroll Deposit ACME INC             1,500.00    2,300.00\n"
        "Closing Balance                                                    2,300.00\n"
        "Page 1 of 1\n"
    )

    def test_parses_transactions(self):
        rows = parse_tangerine_bank_statement_text(self.SAMPLE)
        self.assertEqual(len(rows), 2)

    def test_captures_amount_and_description(self):
        rows = parse_tangerine_bank_statement_text(self.SAMPLE)
        first = rows[0]
        self.assertEqual(first["date"], "05 Nov 2025")
        self.assertEqual(first["description"], "INTERAC e-Transfer To: John Smith")

    def test_withdrawal_is_signed_negative_via_balance_trail(self):
        # Balance drops 1000.00 -> 800.00: this is an outflow, and the
        # amount should be signed negative even though the source text
        # never carries a minus sign or parentheses for this format.
        rows = parse_tangerine_bank_statement_text(self.SAMPLE)
        self.assertEqual(rows[0]["amount"], -200.00)

    def test_deposit_is_signed_positive_via_balance_trail(self):
        # Balance rises 800.00 -> 2,300.00: an inflow, signed positive.
        rows = parse_tangerine_bank_statement_text(self.SAMPLE)
        self.assertEqual(rows[1]["amount"], 1500.00)

    def test_captures_running_balance_for_reconciliation(self):
        rows = parse_tangerine_bank_statement_text(self.SAMPLE)
        self.assertEqual(rows[0]["balance"], 800.00)
        self.assertEqual(rows[1]["balance"], 2300.00)

    def test_skips_opening_and_closing_balance_lines(self):
        rows = parse_tangerine_bank_statement_text(self.SAMPLE)
        descriptions = [r["description"] for r in rows]
        self.assertNotIn("Balance", descriptions)
        self.assertEqual(len(rows), 2)

    def test_account_type_is_bank(self):
        rows = parse_tangerine_bank_statement_text(self.SAMPLE)
        self.assertTrue(all(r["account_type"] == "BANK" for r in rows))

    def test_empty_text_returns_empty_list(self):
        self.assertEqual(parse_tangerine_bank_statement_text(""), [])
        self.assertEqual(parse_tangerine_bank_statement_text(None), [])

    def test_captures_opening_and_closing_balance(self):
        result = _parse_tangerine_bank_statement_full(self.SAMPLE)
        self.assertEqual(result["opening_balance"], Decimal("1000.00"))
        self.assertEqual(result["closing_balance"], Decimal("2300.00"))

    def test_consistent_rows_marked_ok(self):
        result = _parse_tangerine_bank_statement_full(self.SAMPLE)
        self.assertTrue(all(tx["balance_check"] == "ok" for tx in result["transactions"]))

    def test_balance_mismatch_is_flagged(self):
        # Balance jumps from 800 to 2,300 (delta 1,500) but the amount
        # column claims only 100.00 -- something doesn't add up, e.g. a
        # missed transaction between these two rows.
        sample = (
            "Transaction Date  Transaction Description  Amount($)  Balance($)\n"
            "Opening Balance                                        1,000.00\n"
            "05 Nov 2025       Withdrawal                200.00     800.00\n"
            "12 Nov 2025       Payroll Deposit            100.00    2,300.00\n"
        )
        result = _parse_tangerine_bank_statement_full(sample)
        txs = result["transactions"]
        self.assertEqual(txs[0]["balance_check"], "ok")
        self.assertEqual(txs[1]["balance_check"], "mismatch")
        self.assertEqual(len(result["skipped_lines"]), 0)

    def test_no_opening_balance_marks_first_row_unverified(self):
        sample = (
            "Transaction Date  Transaction Description  Amount($)  Balance($)\n"
            "05 Nov 2025       Withdrawal                200.00     800.00\n"
            "12 Nov 2025       Payroll Deposit            1,500.00  2,300.00\n"
        )
        result = _parse_tangerine_bank_statement_full(sample)
        txs = result["transactions"]
        # First row has nothing to compare against
        self.assertEqual(txs[0]["balance_check"], "unverified")
        # Second row can still be checked against the first row's own reported balance
        self.assertEqual(txs[1]["balance_check"], "ok")
        self.assertEqual(txs[1]["amount"], 1500.00)

    def test_wrapped_continuation_line_without_date_is_flagged(self):
        # Regression test: a long description that wraps onto a second
        # line with no date of its own used to vanish silently.
        sample = (
            "Transaction Date  Transaction Description  Amount($)  Balance($)\n"
            "Opening Balance                                        1,000.00\n"
            "18 Nov 2025       Bill Payment - Long Distance Internet    89.99   910.01\n"
            "Subscription Fee\n"
        )
        result = _parse_tangerine_bank_statement_full(sample)
        self.assertEqual(len(result["transactions"]), 1)
        self.assertEqual(len(result["skipped_lines"]), 1)
        self.assertIn("Subscription Fee", result["skipped_lines"][0]["line"])
        self.assertIn("wrapped description", result["skipped_lines"][0]["reason"])

    def test_date_only_line_missing_amount_or_balance_is_flagged(self):
        sample = (
            "Transaction Date  Transaction Description  Amount($)  Balance($)\n"
            "05 Nov 2025       Some transaction with only one number    200.00\n"
        )
        result = _parse_tangerine_bank_statement_full(sample)
        self.assertEqual(result["transactions"], [])
        self.assertEqual(len(result["skipped_lines"]), 1)
        self.assertIn("not both an amount and a balance", result["skipped_lines"][0]["reason"])


class TestRawRowsToImportLines(unittest.TestCase):
    def test_applies_matching_category_rule_to_outflow(self):
        ledger = FakeLedger({"AMAZON": "Office Supplies"})
        raw = [{"description": "AMAZON.CA PURCHASE", "amount": 45.67, "date": "2026-01-05"}]
        lines = raw_rows_to_import_lines(raw, ledger)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["category_account"], "Office Supplies")
        self.assertEqual(lines[0]["direction"], "outflow")

    def test_falls_back_to_uncategorized_expense_when_no_rule_matches(self):
        ledger = FakeLedger({})
        raw = [{"description": "MYSTERY VENDOR", "amount": 12.00, "date": "2026-01-05"}]
        lines = raw_rows_to_import_lines(raw, ledger)
        self.assertEqual(lines[0]["category_account"], "Uncategorized Expense")

    def test_bank_withdrawal_uses_checking_as_contra_and_debits_expense(self):
        ledger = FakeLedger({})
        raw = [{"description": "INTERAC e-Transfer", "amount": -20.00, "date": "05 Nov 2025",
                "account_type": "BANK"}]
        lines = raw_rows_to_import_lines(raw, ledger)
        self.assertEqual(lines[0]["contra_account"], "Checking Account")
        self.assertEqual(lines[0]["direction"], "outflow")
        # Expense account debited, Checking Account credited
        acct, dr, cr = lines[0]["lines"][0]
        self.assertEqual(acct, "Uncategorized Expense")
        self.assertEqual(dr, 20.00)
        self.assertEqual(cr, 0.0)
        acct2, dr2, cr2 = lines[0]["lines"][1]
        self.assertEqual(acct2, "Checking Account")
        self.assertEqual(dr2, 0.0)
        self.assertEqual(cr2, 20.00)

    def test_bank_deposit_debits_checking_and_credits_income(self):
        # This is the case that used to be posted backwards: a positive
        # (deposit) bank-statement amount must debit Checking Account and
        # credit an income account, not the other way around.
        ledger = FakeLedger({"AMAZON": "Office Supplies"})  # should NOT be applied to a deposit
        raw = [{"description": "Payroll Deposit ACME INC (AMAZON in name to test rule isn't applied)",
                "amount": 1500.00, "date": "12 Nov 2025", "account_type": "BANK"}]
        lines = raw_rows_to_import_lines(raw, ledger)
        self.assertEqual(lines[0]["direction"], "inflow")
        self.assertEqual(lines[0]["category_account"], "Uncategorized Income")
        acct, dr, cr = lines[0]["lines"][0]
        self.assertEqual(acct, "Checking Account")
        self.assertEqual(dr, 1500.00)
        self.assertEqual(cr, 0.0)
        acct2, dr2, cr2 = lines[0]["lines"][1]
        self.assertEqual(acct2, "Uncategorized Income")
        self.assertEqual(dr2, 0.0)
        self.assertEqual(cr2, 1500.00)

    def test_credit_card_rows_use_credit_card_contra_account(self):
        ledger = FakeLedger({})
        raw = [{"description": "COFFEE SHOP", "amount": 5.25, "trans_date": "Jan 05"}]
        lines = raw_rows_to_import_lines(raw, ledger)
        self.assertEqual(lines[0]["contra_account"], "Credit Card")

    def test_zero_amount_rows_are_dropped(self):
        ledger = FakeLedger({})
        raw = [{"description": "REVERSED CHARGE", "amount": 0, "date": "2026-01-05"}]
        lines = raw_rows_to_import_lines(raw, ledger)
        self.assertEqual(lines, [])

    def test_statement_balance_is_carried_through(self):
        ledger = FakeLedger({})
        raw = [{"description": "PAYROLL", "amount": 1500.00, "date": "05 Nov 2025",
                "account_type": "BANK", "balance": 2300.00}]
        lines = raw_rows_to_import_lines(raw, ledger)
        self.assertEqual(lines[0]["statement_balance"], 2300.00)

    def test_missing_balance_is_none(self):
        ledger = FakeLedger({})
        raw = [{"description": "COFFEE SHOP", "amount": 5.25, "trans_date": "Jan 05"}]
        lines = raw_rows_to_import_lines(raw, ledger)
        self.assertIsNone(lines[0]["statement_balance"])

    def test_description_truncated_to_80_chars(self):
        ledger = FakeLedger({})
        long_desc = "X" * 200
        raw = [{"description": long_desc, "amount": 1.00, "date": "2026-01-05"}]
        lines = raw_rows_to_import_lines(raw, ledger)
        self.assertEqual(len(lines[0]["description"]), 80)

    def test_balance_check_flag_is_carried_through(self):
        ledger = FakeLedger({})
        raw = [{"description": "Weird row", "amount": 100.00, "date": "05 Nov 2025",
                "account_type": "BANK", "balance": 900.00,
                "balance_check": "mismatch", "balance_check_detail": "off by $50"}]
        lines = raw_rows_to_import_lines(raw, ledger)
        self.assertEqual(lines[0]["balance_check"], "mismatch")
        self.assertEqual(lines[0]["balance_check_detail"], "off by $50")

    def test_posting_lines_always_balance(self):
        ledger = FakeLedger({})
        raw = [
            {"description": "Withdrawal", "amount": -75.50, "date": "05 Nov 2025", "account_type": "BANK"},
            {"description": "Deposit", "amount": 200.00, "date": "06 Nov 2025", "account_type": "BANK"},
            {"description": "Card purchase", "amount": 12.34, "date": "Jan 05", "trans_date": "Jan 05"},
        ]
        lines = raw_rows_to_import_lines(raw, ledger)
        for entry in lines:
            total_dr = sum(dr for _, dr, _ in entry["lines"])
            total_cr = sum(cr for _, _, cr in entry["lines"])
            self.assertAlmostEqual(total_dr, total_cr)


if __name__ == "__main__":
    unittest.main()
